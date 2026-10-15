using System.Diagnostics;
using Worker.Infrastructure;

namespace Worker.Tools;

/// <summary>Fixed-text failure (it becomes the execute_tool span status): <see cref="Reason"/> is <c>timeout</c> or <c>downstream_error</c>.</summary>
public sealed class LookupFailedException(string reason, Exception? inner = null)
    : Exception($"lookup_availability failed: {reason}", inner)
{
    public string Reason { get; } = reason;
}

/// <summary>The MAF agent's local tool: stock (SQL) and shipping quote (HTTP) concurrently, both required.</summary>
public sealed class LookupAvailability(StockRepository stock, ShippingClient shipping, DemoSettings settings, ILogger<LookupAvailability> logger)
{
    public Task<LookupResult> LookupAsync(ProductCode productCode, DestinationZone zone, CancellationToken cancellationToken) =>
        RunAsync(stock.QueryAsync, shipping.GetQuoteAsync, productCode, zone, settings.Scenario, settings.BranchTimeout, logger, cancellationToken);

    internal static async Task<LookupResult> RunAsync(
        Func<ProductCode, CancellationToken, Task<StockLevel>> queryStock,
        Func<DestinationZone, CancellationToken, Task<ShippingQuote>> fetchQuote,
        ProductCode productCode,
        DestinationZone zone,
        string scenario,
        TimeSpan branchTimeout,
        ILogger logger,
        CancellationToken cancellationToken)
    {
        // Called inside the framework's "execute_tool lookup_availability" span (Activity.Current);
        // both branches are created while it is current, and it stays open until both have finished.
        using var branchCts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        branchCts.CancelAfter(branchTimeout);
        CancellationToken token = branchCts.Token;
        long started = Stopwatch.GetTimestamp();

        Task<StockLevel> stockTask = Branch("sql", () => queryStock(productCode, token), logger);
        if (scenario == Scenarios.SerialIo)
        {
            // Fault (serial-io): finish SQL before the HTTP branch exists. A failure is observed at the join below.
            await ((Task)stockTask).ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
        }

        Task<ShippingQuote> quoteTask;
        if (scenario == Scenarios.ContextDetached)
        {
            // Fault (context-detached): create the HTTP branch without flowing the ExecutionContext, so it runs with
            // no Activity.Current and its HTTP client span becomes a new root.
            using (ExecutionContext.SuppressFlow())
            {
                quoteTask = Detached(() => Branch("http", () => fetchQuote(zone, token), logger));
            } // <- flow restored HERE, immediately after the task is created (Detached returns at its first yield, on this thread).
        }
        else
        {
            quoteTask = Branch("http", () => fetchQuote(zone, token), logger);
        }

        string outcome = "ok";
        try
        {
            await Task.WhenAll(stockTask, quoteTask); // observes both tasks; WhenAll never cancels the sibling
            return new LookupResult(await stockTask, await quoteTask);
        }
        catch (Exception ex)
        {
            outcome = cancellationToken.IsCancellationRequested ? "canceled"
                : IsDownstreamFault(stockTask) || IsDownstreamFault(quoteTask) ? "downstream_error"
                : "timeout";
            if (outcome == "canceled")
            {
                throw new OperationCanceledException("lookup_availability canceled by caller", ex, cancellationToken);
            }
            throw new LookupFailedException(outcome, ex);
        }
        finally
        {
            Activity.Current?.AddEvent(new ActivityEvent("lookup_availability.join"));
            logger.LogInformation(
                "lookup_availability join {demo.outcome} {demo.duration_ms}", outcome, Stopwatch.GetElapsedTime(started).TotalMilliseconds);
        }
    }

    private static bool IsDownstreamFault(Task task) =>
        task.IsFaulted && task.Exception!.InnerException is not OperationCanceledException;

    private static async Task<T> Branch<T>(string branch, Func<Task<T>> run, ILogger logger)
    {
        long started = Stopwatch.GetTimestamp();
        try
        {
            T result = await run();
            logger.LogInformation(
                "lookup_availability branch completed {demo.branch} {demo.duration_ms}", branch, Stopwatch.GetElapsedTime(started).TotalMilliseconds);
            return result;
        }
        catch (Exception ex)
        {
            logger.LogWarning(
                "lookup_availability branch failed {demo.branch} {demo.duration_ms} {error.type}",
                branch, Stopwatch.GetElapsedTime(started).TotalMilliseconds, ex.GetType().FullName);
            throw;
        }
    }

    private static async Task<T> Detached<T>(Func<Task<T>> start)
    {
        // SuppressFlow alone is not enough: HttpClient starts its Activity synchronously, before any await, while
        // Activity.Current is still the tool span. Yielding first moves the start onto a thread-pool continuation
        // that captured no ExecutionContext. This is a yield, not Task.Run: the I/O itself stays a plain async call.
        await Task.Yield();
        return await start();
    }
}
