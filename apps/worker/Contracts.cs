using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.Json.Serialization.Metadata;

namespace Worker;

// Wire shapes from contracts/report.schema.json. snake_case names are explicit so every serializer
// (MCP SDK, Microsoft.Extensions.AI, HttpClient JSON, tests) produces the same JSON.

[JsonConverter(typeof(JsonStringEnumConverter<ProductCode>))]
public enum ProductCode
{
    [JsonStringEnumMemberName("DEMO-LANTERN")] DemoLantern,
    [JsonStringEnumMemberName("DEMO-KETTLE")] DemoKettle,
    [JsonStringEnumMemberName("DEMO-TENT")] DemoTent,
}

[JsonConverter(typeof(JsonStringEnumConverter<DestinationZone>))]
public enum DestinationZone
{
    [JsonStringEnumMemberName("DEMO-NORTH")] DemoNorth,
    [JsonStringEnumMemberName("DEMO-SOUTH")] DemoSouth,
    [JsonStringEnumMemberName("DEMO-EAST")] DemoEast,
}

public sealed record StockLevel(
    [property: JsonPropertyName("product_code")] ProductCode ProductCode,
    [property: JsonPropertyName("quantity_on_hand")] int QuantityOnHand,
    [property: JsonPropertyName("warehouse")] string Warehouse);

public sealed record ShippingQuote(
    [property: JsonPropertyName("destination_zone")] DestinationZone DestinationZone,
    [property: JsonPropertyName("carrier")] string Carrier,
    [property: JsonPropertyName("price_cents")] int PriceCents,
    [property: JsonPropertyName("currency")] string Currency,
    [property: JsonPropertyName("eta_days")] int EtaDays,
    [property: JsonPropertyName("reference")] string Reference);

public sealed record LookupResult(
    [property: JsonPropertyName("stock")] StockLevel Stock,
    [property: JsonPropertyName("quote")] ShippingQuote Quote);

public sealed record AvailabilityReport(
    [property: JsonPropertyName("product_code")] ProductCode ProductCode,
    [property: JsonPropertyName("destination_zone")] DestinationZone DestinationZone,
    [property: JsonPropertyName("available")] bool Available,
    [property: JsonPropertyName("quantity_on_hand")] int QuantityOnHand,
    [property: JsonPropertyName("warehouse")] string Warehouse,
    [property: JsonPropertyName("shipping")] ShippingQuote Shipping,
    [property: JsonPropertyName("summary")] string Summary);

public static class Json
{
    /// <summary>
    /// Strict options for untrusted input (shipping responses, model output): general (not web) defaults, so names are
    /// case-sensitive and numbers-as-strings are rejected; missing, null or unknown members fail.
    /// </summary>
    public static readonly JsonSerializerOptions Strict = new()
    {
        TypeInfoResolver = new DefaultJsonTypeInfoResolver(),
        RespectNullableAnnotations = true,
        RespectRequiredConstructorParameters = true,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
    };

    /// <summary>The contract string for an enum value, e.g. <c>DEMO-NORTH</c>.</summary>
    public static string Wire<T>(T value) where T : struct, Enum => JsonSerializer.Serialize(value).Trim('"');

    /// <summary>
    /// Exact, ordinal match against the contract strings. Used at the tool boundaries instead of enum binding,
    /// because JsonStringEnumConverter also accepts integers and comma-combined names ("DEMO-LANTERN, DEMO-TENT").
    /// </summary>
    public static bool TryParseWire<T>(string? wire, out T value) where T : struct, Enum
    {
        foreach (T candidate in Enum.GetValues<T>())
        {
            if (string.Equals(Wire(candidate), wire, StringComparison.Ordinal))
            {
                value = candidate;
                return true;
            }
        }
        value = default;
        return false;
    }
}
