namespace Worker;

public static class Scenarios
{
    public const string Baseline = "baseline";
    public const string ContextDetached = "context-detached";
    public const string SerialIo = "serial-io";
    public const string LogicalError = "logical-error";

    // Scenarios owned by other services are accepted and ignored here.
    public static readonly string[] All =
        [Baseline, "headers-removed", "meta-removed", "both-removed", ContextDetached, SerialIo, LogicalError];
}

/// <summary>Explicit readers for the repo's DEMO_* contract (these are not vendor options).</summary>
public sealed record DemoSettings(
    string Mode,
    string Scenario,
    bool ContentCapture,
    string DbConnection,
    Uri ShippingUrl,
    TimeSpan SqlDelay,
    TimeSpan BranchTimeout,
    string ModelId,
    int MaxOutputTokens,
    string? AnthropicApiKey,
    Uri? AnthropicBaseUrl)
{
    public bool IsLive => Mode == "live";

    // The generated record ToString would print the DB password and API key.
    public override string ToString() => $"DemoSettings {{ Mode = {Mode}, Scenario = {Scenario} }}";

    public static DemoSettings FromConfiguration(IConfiguration config)
    {
        string mode = config["DEMO_MODE"] ?? "scripted";
        if (mode is not ("scripted" or "live"))
        {
            throw new InvalidOperationException($"DEMO_MODE must be 'scripted' or 'live', got '{mode}'.");
        }
        string scenario = config["DEMO_SCENARIO"] ?? Scenarios.Baseline;
        if (!Scenarios.All.Contains(scenario))
        {
            throw new InvalidOperationException($"Unknown DEMO_SCENARIO '{scenario}'. Expected one of: {string.Join(", ", Scenarios.All)}.");
        }
        string? apiKey = config["ANTHROPIC_API_KEY"];
        if (mode == "live" && string.IsNullOrWhiteSpace(apiKey))
        {
            throw new InvalidOperationException("DEMO_MODE=live requires ANTHROPIC_API_KEY.");
        }
        string? baseUrl = config["ANTHROPIC_BASE_URL"];

        return new DemoSettings(
            mode,
            scenario,
            bool.Parse(config["DEMO_CONTENT_CAPTURE"] ?? "false"),
            Required(config, "DEMO_DB_CONNECTION"),
            new Uri(Required(config, "DEMO_SHIPPING_URL").TrimEnd('/') + "/", UriKind.Absolute),
            TimeSpan.FromMilliseconds(int.Parse(config["DEMO_SQL_DELAY_MS"] ?? "300")),
            TimeSpan.FromMilliseconds(int.Parse(config["DEMO_BRANCH_TIMEOUT_MS"] ?? "5000")),
            config["DEMO_MODEL_ID"] ?? "claude-haiku-5-5",
            int.Parse(config["DEMO_MAX_OUTPUT_TOKENS"] ?? "512"),
            apiKey,
            string.IsNullOrWhiteSpace(baseUrl) ? null : new Uri(baseUrl, UriKind.Absolute));
    }

    private static string Required(IConfiguration config, string key) =>
        config[key] is { Length: > 0 } value ? value : throw new InvalidOperationException($"{key} is required.");
}
