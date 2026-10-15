using System.Diagnostics;
using Worker.Agents;
using Worker.Tools;

namespace Worker.Tests;

public class AnalystTests
{
    private static readonly LookupResult _lookup = new(
        new StockLevel(ProductCode.DemoTent, 3, "DEMO-WH-1"),
        new ShippingQuote(DestinationZone.DemoEast, "DEMO-CARRIER", 1500, "EUR", 4, "ref"));

    [Fact]
    public async Task Scripted_model_drives_the_real_agent_loop_which_calls_lookup_once()
    {
        // Capture only this test's spans (other tests may run agents concurrently).
        using var root = new ActivitySource("Worker.Tests.Analyst");
        var spans = new List<Activity>();
        ActivityTraceId traceId = default;
        using var listener = new ActivityListener
        {
            ShouldListenTo = s => s == root || s.Name is AvailabilityAnalyst.AgentSourceName or AvailabilityAnalyst.ChatSourceName,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
            ActivityStopped = a =>
            {
                lock (spans)
                {
                    if (a.TraceId == traceId)
                    {
                        spans.Add(a);
                    }
                }
            },
        };
        ActivitySource.AddActivityListener(listener);
        int calls = 0;

        AvailabilityReport report;
        using (Activity parent = root.StartActivity("test")!)
        {
            traceId = parent.TraceId;
            report = await AvailabilityAnalyst.RunAsync(
                new ScriptedChatClient(ProductCode.DemoTent, DestinationZone.DemoEast),
                (product, zone, _) =>
                {
                    calls++;
                    Assert.Equal((ProductCode.DemoTent, DestinationZone.DemoEast), (product, zone));
                    return Task.FromResult(_lookup);
                },
                ProductCode.DemoTent, DestinationZone.DemoEast, enableSensitiveData: false, maxOutputTokens: 512,
                TestContext.Current.CancellationToken);
        }

        Assert.Equal(1, calls);
        Assert.Equal(
            new AvailabilityReport(ProductCode.DemoTent, DestinationZone.DemoEast, true, 3, "DEMO-WH-1", _lookup.Quote, report.Summary),
            report);
        Assert.Contains(ScriptedChatClient.SummaryCanary, report.Summary);

        // One owner per layer: one agent span, one framework tool span, one chat span per model call; no usage invented.
        string Names(string source) =>
            string.Join(" | ", spans.Where(s => s.Source.Name == source).Select(s => s.DisplayName).Order(StringComparer.Ordinal));
        Assert.Equal(
            "execute_tool lookup_availability | invoke_agent availability-analyst(availability-analyst)",
            Names(AvailabilityAnalyst.AgentSourceName));
        Assert.Equal("chat scripted-analyst | chat scripted-analyst", Names(AvailabilityAnalyst.ChatSourceName));
        Assert.DoesNotContain(spans, s => s.TagObjects.Any(t => t.Key.StartsWith("gen_ai.usage", StringComparison.Ordinal)));
    }

    [Fact]
    public async Task Lookup_failure_surfaces_as_the_original_exception_not_as_model_text()
    {
        LookupFailedException ex = await Assert.ThrowsAsync<LookupFailedException>(() => AvailabilityAnalyst.RunAsync(
            new ScriptedChatClient(ProductCode.DemoKettle, DestinationZone.DemoSouth),
            (_, _, _) => Task.FromException<LookupResult>(new LookupFailedException("timeout")),
            ProductCode.DemoKettle, DestinationZone.DemoSouth, enableSensitiveData: false, maxOutputTokens: 512,
            TestContext.Current.CancellationToken));
        Assert.Equal("timeout", ex.Reason);
    }
}
