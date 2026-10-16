using System.ComponentModel.DataAnnotations;
using System.Runtime.ExceptionServices;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;

namespace Worker.Agents;

/// <summary>Model for one tool invocation: scripted (per validated args) or the shared live Anthropic client.</summary>
public delegate IChatClient AnalystModelFactory(ProductCode productCode, DestinationZone zone);

/// <summary>The MAF agent <c>availability-analyst</c> with its single local tool <c>lookup_availability</c>.</summary>
public static class AvailabilityAnalyst
{
    public const string Name = "availability-analyst";

    // Passed explicitly (they equal the framework defaults) and registered with AddSource in WorkerTelemetry.
    public const string AgentSourceName = "Experimental.Microsoft.Agents.AI";
    public const string ChatSourceName = "Experimental.Microsoft.Extensions.AI";

    private const string Instructions =
        "You are availability-analyst. Call lookup_availability exactly once with the product_code and destination_zone " +
        "you are given, then answer with only an AvailabilityReport JSON object built from its result.";

    public static async Task<AvailabilityReport> RunAsync(
        IChatClient model,
        Func<ProductCode, DestinationZone, CancellationToken, Task<LookupResult>> lookup,
        ProductCode productCode,
        DestinationZone zone,
        int maxOutputTokens,
        CancellationToken cancellationToken)
    {
        int lookupCalls = 0;
        Exception? lookupFailure = null;
        AIFunction lookupTool = AIFunctionFactory.Create(
            async (
                [AllowedValues("DEMO-LANTERN", "DEMO-KETTLE", "DEMO-TENT")] string product_code,
                [AllowedValues("DEMO-NORTH", "DEMO-SOUTH", "DEMO-EAST")] string destination_zone,
                CancellationToken ct) =>
            {
                lookupCalls++;
                // Model-chosen arguments are untrusted: exact enum strings only. They select a seeded row and a
                // fixed-host path segment, never a URL or SQL text.
                if (!Json.TryParseWire(product_code, out ProductCode p) || !Json.TryParseWire(destination_zone, out DestinationZone z))
                {
                    throw new ArgumentException("lookup_availability arguments are outside the allowed enumerations");
                }
                try
                {
                    return await lookup(p, z, ct);
                }
                catch (Exception ex)
                {
                    lookupFailure = ex; // FunctionInvokingChatClient hands the model a generic error; keep the real one
                    throw;
                }
            },
            new AIFunctionFactoryOptions
            {
                Name = "lookup_availability",
                Description = "Look up stock on hand and a shipping quote for one demo product and destination zone.",
                SerializerOptions = Json.Strict,
            });

        // Chat client: leaf model -> OpenTelemetryChatClient (one "chat" span per model call) -> FunctionInvokingChatClient
        // (capped loop). ChatClientAgent reuses this FICC instead of adding its own, and OpenTelemetryAgent sees the
        // chat client is already instrumented so it does not auto-wire a second chat span.
        IChatClient chatClient = model.AsBuilder()
            .UseFunctionInvocation(configure: f => f.MaximumIterationsPerRequest = 3)
            // Messages are always recorded; GenAiContentRedactor reduces them to their shape before export unless
            // DEMO_CONTENT_CAPTURE is on (Pydantic AI-style redaction instead of omitting them).
            .UseOpenTelemetry(sourceName: ChatSourceName, configure: o => o.EnableSensitiveData = true)
            .Build();

        AIAgent agent = new ChatClientAgent(chatClient, new ChatClientAgentOptions
        {
            Id = Name, // the span name is "invoke_agent {Name}({Id})"; a fixed Id keeps it stable per request
            Name = Name,
            ChatOptions = new ChatOptions { Instructions = Instructions, Tools = [lookupTool], MaxOutputTokens = maxOutputTokens },
        })
            .AsBuilder()
            .UseOpenTelemetry(AgentSourceName, a => a.EnableSensitiveData = true)
            .Build();

        AgentResponse<AvailabilityReport> response = await agent.RunAsync<AvailabilityReport>(
            $"Build the availability report for product_code {Json.Wire(productCode)} and destination_zone {Json.Wire(zone)}.",
            serializerOptions: Json.Strict,
            cancellationToken: cancellationToken);

        if (lookupFailure is not null)
        {
            ExceptionDispatchInfo.Throw(lookupFailure);
        }
        if (lookupCalls == 0)
        {
            throw new InvalidOperationException("lookup_availability was not invoked"); // live mode: a missed tool call is a workload failure
        }
        AvailabilityReport report = response.Result; // throws if the final answer is not a valid report
        return report.ProductCode == productCode && report.DestinationZone == zone
            ? report
            : throw new InvalidOperationException("report does not match the requested product and zone");
    }
}
