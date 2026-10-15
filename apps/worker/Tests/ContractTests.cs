using System.Reflection;
using System.Text.Json.Nodes;
using System.Text.Json.Schema;
using Microsoft.Extensions.Configuration;
using ModelContextProtocol.Server;
using Worker.McpTools;

namespace Worker.Tests;

/// <summary>Semantic equivalence (property names, required, types, enums) with contracts/report.schema.json.</summary>
public class ContractTests
{
    private static readonly JsonObject _defs =
        JsonNode.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "report.schema.json")))!["$defs"]!.AsObject();

    [Theory]
    [InlineData("StockLevel", typeof(StockLevel))]
    [InlineData("ShippingQuote", typeof(ShippingQuote))]
    [InlineData("LookupResult", typeof(LookupResult))]
    [InlineData("AvailabilityReport", typeof(AvailabilityReport))]
    public void Dto_matches_contract(string def, Type type) =>
        AssertEquivalent(_defs[def]!, Json.Strict.GetJsonSchemaAsNode(type, new() { TreatNullObliviousAsNonNullable = true }), def);

    [Fact]
    public void Advertised_mcp_tool_schemas_match_contract()
    {
        MethodInfo method = typeof(AvailabilityTools).GetMethod(nameof(AvailabilityTools.BuildAvailabilityReport))!;
        McpServerTool tool = McpServerTool.Create(method, _ => throw new NotSupportedException());

        Assert.Equal("build_availability_report", tool.ProtocolTool.Name);
        AssertEquivalent(_defs["BuildAvailabilityReportArgs"]!, JsonNode.Parse(tool.ProtocolTool.InputSchema.GetRawText())!, "inputSchema");
        AssertEquivalent(_defs["AvailabilityReport"]!, JsonNode.Parse(tool.ProtocolTool.OutputSchema!.Value.GetRawText())!, "outputSchema");
    }

    [Theory]
    [InlineData("DEMO-LANTERN", true)]
    [InlineData("DEMO-TENT", true)]
    [InlineData("demo-lantern", false)]
    [InlineData("DEMO-LANTERN, DEMO-TENT", false)]
    [InlineData("1", false)]
    [InlineData("DemoLantern", false)]
    [InlineData("http://evil.example/quotes", false)]
    [InlineData("DEMO-LANTERN'; DROP TABLE stock;--", false)]
    [InlineData("", false)]
    public void Tool_boundary_accepts_only_exact_contract_strings(string input, bool accepted) =>
        Assert.Equal(accepted, Json.TryParseWire(input, out ProductCode _));

    [Fact]
    public void Unknown_scenario_fails_fast()
    {
        IConfiguration config = new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
        {
            ["DEMO_SCENARIO"] = "not-a-scenario",
            ["DEMO_DB_CONNECTION"] = "Host=x",
            ["DEMO_SHIPPING_URL"] = "http://shipping:8000",
        }).Build();
        Assert.Throws<InvalidOperationException>(() => DemoSettings.FromConfiguration(config));
    }

    private static void AssertEquivalent(JsonNode contract, JsonNode actual, string path)
    {
        if (contract["$ref"] is JsonNode reference)
        {
            contract = _defs[reference.GetValue<string>().Split('/')[^1]]!;
        }
        if (contract["enum"] is JsonArray expectedEnum)
        {
            Assert.Equal($"{path}: {Sorted(expectedEnum)}", $"{path}: {Sorted(actual["enum"]?.AsArray())}");
            return;
        }
        Assert.Equal($"{path}: {contract["type"]}", $"{path}: {TypeOf(actual)}");
        if (contract["properties"] is not JsonObject properties)
        {
            return;
        }
        JsonObject actualProperties = actual["properties"]!.AsObject();
        Assert.Equal(
            $"{path}.properties: {Sorted(properties.Select(p => p.Key))}",
            $"{path}.properties: {Sorted(actualProperties.Select(p => p.Key))}");
        Assert.Equal(
            $"{path}.required: {Sorted(contract["required"]?.AsArray())}",
            $"{path}.required: {Sorted(actual["required"]?.AsArray())}");
        foreach ((string name, JsonNode? property) in properties)
        {
            AssertEquivalent(property!, actualProperties[name]!, $"{path}.{name}");
        }
    }

    // A nullable member would show up as ["string","null"] and fail the comparison, which is intended.
    private static string TypeOf(JsonNode schema) => schema["type"] switch
    {
        JsonArray types => string.Join(",", types.Select(t => t!.GetValue<string>())),
        JsonNode type => type.GetValue<string>(),
        null => "(none)",
    };

    private static string Sorted(IEnumerable<string> values) => string.Join(",", values.Order(StringComparer.Ordinal));

    private static string Sorted(JsonArray? values) => values is null ? "(none)" : Sorted(values.Select(v => v!.GetValue<string>()));
}
