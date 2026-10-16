using Npgsql;
using OpenTelemetry.Logs;
using OpenTelemetry.Trace;
using Worker.Agents;

namespace Worker.Telemetry;

public static class WorkerTelemetry
{
    public const string McpSourceName = "Experimental.ModelContextProtocol";

    /// <summary>
    /// One TracerProvider + one LoggerProvider, both exporting OTLP. Endpoint, protocol, service name and resource
    /// attributes come from the standard OTEL_* environment variables, which the .NET SDK reads itself.
    /// </summary>
    public static void AddWorkerTelemetry(this WebApplicationBuilder builder, bool contentCapture)
    {
        builder.Logging
            .AddFilter("Microsoft.AspNetCore", LogLevel.Warning)
            .AddFilter("System.Net.Http", LogLevel.Warning);

        builder.Services.AddOpenTelemetry()
            .WithTracing(tracing =>
            {
                tracing
                    .AddAspNetCoreInstrumentation(o => o.Filter = ctx => !ctx.Request.Path.StartsWithSegments("/healthz"))
                    .AddHttpClientInstrumentation()
                    .AddNpgsql()
                    .AddSource(McpSourceName, AvailabilityAnalyst.AgentSourceName, AvailabilityAnalyst.ChatSourceName);
                if (!contentCapture)
                {
                    tracing.AddProcessor(new GenAiContentRedactor()); // must precede the exporter's processor
                }
                tracing.AddOtlpExporter();
            })
            .WithLogging(logging => logging.AddOtlpExporter());
    }
}
