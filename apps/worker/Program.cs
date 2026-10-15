using Anthropic;
using Microsoft.Extensions.AI;
using Npgsql;
using Worker;
using Worker.Agents;
using Worker.Infrastructure;
using Worker.McpTools;
using Worker.Telemetry;
using Worker.Tools;

WebApplicationBuilder builder = WebApplication.CreateBuilder(args);
DemoSettings settings = DemoSettings.FromConfiguration(builder.Configuration); // fails fast on bad DEMO_* values
builder.Services.AddSingleton(settings);
builder.AddWorkerTelemetry();

builder.Services.AddSingleton(_ => NpgsqlDataSource.Create(settings.DbConnection));
builder.Services.AddSingleton<StockRepository>();
builder.Services.AddHttpClient<ShippingClient>(http => http.BaseAddress = settings.ShippingUrl)
    .ConfigurePrimaryHttpMessageHandler(() => new SocketsHttpHandler { AllowAutoRedirect = false });
builder.Services.AddTransient<LookupAvailability>();
builder.Services.AddSingleton(settings.IsLive ? LiveModel(settings) : (product, zone) => new ScriptedChatClient(product, zone));

builder.Services.AddMcpServer().WithHttpTransport().WithTools<AvailabilityTools>();

WebApplication app = builder.Build();
app.MapGet("/healthz", () => "ok");
app.MapMcp("/mcp");
app.Run();

static AnalystModelFactory LiveModel(DemoSettings settings)
{
    var anthropic = new AnthropicClient
    {
        ApiKey = settings.AnthropicApiKey!,
        BaseUrl = settings.AnthropicBaseUrl?.ToString() ?? Anthropic.Core.EnvironmentUrl.Production,
        OpenTelemetry = false, // the M.E.AI OpenTelemetryChatClient owns model spans; avoid a second, SDK-native span
    };
    IChatClient client = anthropic.AsIChatClient(settings.ModelId, settings.MaxOutputTokens);
    return (_, _) => client;
}
