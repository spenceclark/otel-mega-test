using System.Diagnostics;
using Microsoft.Extensions.Logging.Abstractions;
using Worker.Tools;

namespace Worker.Tests;

public class LookupAvailabilityTests
{
    private static readonly StockLevel _stock = new(ProductCode.DemoLantern, 12, "DEMO-WH-1");
    private static readonly ShippingQuote _quote = new(DestinationZone.DemoNorth, "DEMO-CARRIER", 1200, "EUR", 2, "ref");

    /// <summary>A fake branch that stays in flight until the test completes it, and records how it was started.</summary>
    private sealed class FakeBranch<T>
    {
        public TaskCompletionSource<T> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public bool Started { get; private set; }
        public CancellationToken Token { get; private set; }
        public Activity? ActivityAtStart { get; private set; }

        public Task<T> Run(CancellationToken token)
        {
            (Started, Token, ActivityAtStart) = (true, token, Activity.Current);
            token.Register(() => Completion.TrySetCanceled(token));
            return Completion.Task;
        }
    }

    private readonly FakeBranch<StockLevel> _sql = new();
    private readonly FakeBranch<ShippingQuote> _http = new();

    private Task<LookupResult> Start(string scenario, CancellationToken? cancellationToken = null, int timeoutMs = 5000) =>
        LookupAvailability.RunAsync(
            (_, token) => _sql.Run(token), (_, token) => _http.Run(token), ProductCode.DemoLantern, DestinationZone.DemoNorth,
            scenario, TimeSpan.FromMilliseconds(timeoutMs), NullLogger.Instance, cancellationToken ?? TestContext.Current.CancellationToken);

    private static async Task WaitUntil(Func<bool> condition)
    {
        for (int i = 0; i < 500 && !condition(); i++)
        {
            await Task.Delay(10, TestContext.Current.CancellationToken);
        }
        Assert.True(condition());
    }

    [Fact]
    public async Task Baseline_starts_both_branches_before_either_completes_and_joins_both()
    {
        Task<LookupResult> run = Start(Scenarios.Baseline);

        Assert.True(_sql.Started && _http.Started); // both in flight at once: the overlap is structural
        _http.Completion.SetResult(_quote);
        await Task.Delay(50, TestContext.Current.CancellationToken);
        Assert.False(run.IsCompleted); // the join still waits for SQL
        _sql.Completion.SetResult(_stock);

        Assert.Equal(new LookupResult(_stock, _quote), await run);
    }

    [Fact]
    public async Task Serial_io_does_not_start_http_until_sql_finishes()
    {
        Task<LookupResult> run = Start(Scenarios.SerialIo);

        Assert.True(_sql.Started);
        Assert.False(_http.Started);
        _sql.Completion.SetResult(_stock);
        await WaitUntil(() => _http.Started);
        _http.Completion.SetResult(_quote);

        Assert.Equal(new LookupResult(_stock, _quote), await run);
    }

    [Fact]
    public async Task A_failing_branch_still_waits_for_and_observes_the_other()
    {
        Task<LookupResult> run = Start(Scenarios.Baseline);

        _sql.Completion.SetException(new InvalidOperationException("db down"));
        await Task.Delay(50, TestContext.Current.CancellationToken);
        Assert.False(run.IsCompleted); // HTTP still in flight; WhenAll does not cancel the sibling
        _http.Completion.SetResult(_quote);

        LookupFailedException ex = await Assert.ThrowsAsync<LookupFailedException>(() => run);
        Assert.Equal("downstream_error", ex.Reason);
    }

    [Fact]
    public async Task Caller_cancellation_reaches_both_branches_and_is_not_reported_as_timeout()
    {
        using var caller = new CancellationTokenSource();
        Task<LookupResult> run = Start(Scenarios.Baseline, caller.Token);

        await caller.CancelAsync();

        OperationCanceledException ex = await Assert.ThrowsAnyAsync<OperationCanceledException>(() => run);
        Assert.Equal(caller.Token, ex.CancellationToken);
        Assert.True(_sql.Token.IsCancellationRequested && _http.Token.IsCancellationRequested);
    }

    [Fact]
    public async Task Branch_timeout_cancels_both_and_is_reported_as_timeout()
    {
        Task<LookupResult> run = Start(Scenarios.Baseline, timeoutMs: 50);

        LookupFailedException ex = await Assert.ThrowsAsync<LookupFailedException>(() => run);
        Assert.Equal("timeout", ex.Reason);
        Assert.True(_sql.Token.IsCancellationRequested && _http.Token.IsCancellationRequested);
    }

    [Theory]
    [InlineData(Scenarios.Baseline, false)]
    [InlineData(Scenarios.ContextDetached, true)]
    public async Task Http_branch_loses_the_tool_activity_only_when_detached(string scenario, bool detached)
    {
        using var source = new ActivitySource("Worker.Tests.Lookup");
        using var listener = new ActivityListener
        {
            ShouldListenTo = s => s == source,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(listener);
        using Activity tool = source.StartActivity("execute_tool lookup_availability")!;

        Task<LookupResult> run = Start(scenario);
        await WaitUntil(() => _http.Started);

        Assert.Same(tool, _sql.ActivityAtStart);
        Assert.Same(detached ? null : tool, _http.ActivityAtStart);
        Assert.Same(tool, Activity.Current); // ambient context restored right after the detached task was created
        _sql.Completion.SetResult(_stock);
        _http.Completion.SetResult(_quote);
        await run;
    }
}
