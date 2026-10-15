using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Diagnostics;
using ModelContextProtocol;
using ModelContextProtocol.Server;
using Worker.Agents;
using Worker.Tools;

namespace Worker.McpTools;

[McpServerToolType]
public sealed class AvailabilityTools(
    LookupAvailability lookup, AnalystModelFactory modelFactory, DemoSettings settings, ILogger<AvailabilityTools> logger)
{
    // Error results are McpException -> CallToolResult { isError = true } with HTTP 200. The SDK copies this text into
    // the tools/call span status, so it is fixed text with a stable leading code and never arguments or results.
    [McpServerTool(Name = "build_availability_report", UseStructuredContent = true, ReadOnly = true, Idempotent = true, OpenWorld = false)]
    [Description("Build a stock and shipping availability report for one demo product and destination zone.")]
    public async Task<AvailabilityReport> BuildAvailabilityReport(
        [Description("Seeded demo product code."), AllowedValues("DEMO-LANTERN", "DEMO-KETTLE", "DEMO-TENT")] string product_code,
        [Description("Seeded demo destination zone."), AllowedValues("DEMO-NORTH", "DEMO-SOUTH", "DEMO-EAST")] string destination_zone,
        CancellationToken cancellationToken)
    {
        // Activity.Current here is the MCP SDK's "tools/call build_availability_report" SERVER span.
        Activity? span = Activity.Current;
        span?.SetTag("demo.scenario", settings.Scenario).SetTag("demo.mode", settings.Mode);
        // Trust boundary: exact enum strings only (AllowedValues only shapes the advertised schema). URLs, SQL,
        // integers, other casing and comma-combined names are rejected before any work starts.
        if (!Json.TryParseWire(product_code, out ProductCode productCode) || !Json.TryParseWire(destination_zone, out DestinationZone zone))
        {
            span?.SetTag("demo.outcome", "error");
            throw new McpException("invalid_arguments: product_code or destination_zone is not an allowed value");
        }

        logger.LogInformation("build_availability_report started {demo.scenario} {demo.mode}", settings.Scenario, settings.Mode);
        long started = Stopwatch.GetTimestamp();
        string outcome = "error";
        string? errorCode = null;
        try
        {
            AvailabilityReport report = await AvailabilityAnalyst.RunAsync(
                modelFactory(productCode, zone), lookup.LookupAsync, productCode, zone,
                settings.ContentCapture, settings.MaxOutputTokens, cancellationToken);
            if (settings.Scenario == Scenarios.LogicalError)
            {
                // Fault (logical-error): the work succeeded, but the tool reports a logical failure under HTTP 200.
                (outcome, errorCode) = ("tool_error", "tool_error");
                throw new McpException("tool_error: report withheld by the logical-error scenario");
            }
            outcome = "ok";
            return report;
        }
        catch (LookupFailedException ex)
        {
            errorCode = ex.Reason;
            throw new McpException($"{ex.Reason}: lookup_availability failed");
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            errorCode = "canceled";
            throw; // the SDK rethrows caller cancellation instead of producing a result
        }
        catch (Exception ex) when (ex is not McpException)
        {
            errorCode = "workload_failure";
            logger.LogWarning("build_availability_report workload failure {error.type}", ex.GetType().FullName); // type only, no message
            throw new McpException("workload_failure: availability-analyst did not produce a valid report");
        }
        finally
        {
            span?.SetTag("demo.outcome", outcome);
            logger.LogInformation(
                "build_availability_report completed {demo.outcome} {demo.error_code} {demo.duration_ms}",
                outcome, errorCode, Stopwatch.GetElapsedTime(started).TotalMilliseconds);
        }
    }
}
