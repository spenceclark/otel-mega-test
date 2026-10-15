using System.Text.Json;
using Microsoft.Extensions.AI;

namespace Worker.Agents;

/// <summary>
/// Deterministic model for DEMO_MODE=scripted, injected at the IChatClient abstraction so the real
/// ChatClientAgent + FunctionInvokingChatClient loop runs. Call 1 asks for lookup_availability; call 2 turns its
/// result into the report JSON. Usage is left null: nothing is fabricated.
/// </summary>
public sealed class ScriptedChatClient(ProductCode productCode, DestinationZone zone) : IChatClient
{
    public const string SummaryCanary = "CANARY-ANALYST-0f2b";
    private static readonly ChatClientMetadata _metadata = new("scripted", null, "scripted-analyst");

    public Task<ChatResponse> GetResponseAsync(
        IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default)
    {
        FunctionResultContent? toolResult = messages.SelectMany(m => m.Contents).OfType<FunctionResultContent>().LastOrDefault();
        ChatMessage reply = toolResult is null
            ? new ChatMessage(ChatRole.Assistant, [new FunctionCallContent("scripted-call-1", "lookup_availability", new Dictionary<string, object?>
            {
                ["product_code"] = Json.Wire(productCode),
                ["destination_zone"] = Json.Wire(zone),
            })])
            : new ChatMessage(ChatRole.Assistant, ReportJson(toolResult) ?? "lookup_availability failed; no report.");
        return Task.FromResult(new ChatResponse(reply)
        {
            ModelId = _metadata.DefaultModelId,
            FinishReason = toolResult is null ? ChatFinishReason.ToolCalls : ChatFinishReason.Stop,
        });
    }

    private static string? ReportJson(FunctionResultContent toolResult)
    {
        LookupResult? lookup = toolResult.Exception is not null ? null : toolResult.Result switch
        {
            LookupResult r => r,
            JsonElement e => e.Deserialize<LookupResult>(Json.Strict),
            _ => null,
        };
        if (lookup is null)
        {
            return null;
        }
        (StockLevel stock, ShippingQuote quote) = (lookup.Stock, lookup.Quote);
        var report = new AvailabilityReport(
            stock.ProductCode,
            quote.DestinationZone,
            stock.QuantityOnHand > 0,
            stock.QuantityOnHand,
            stock.Warehouse,
            quote,
            $"{Json.Wire(stock.ProductCode)}: {stock.QuantityOnHand} on hand at {stock.Warehouse}; " +
            $"ships to {Json.Wire(quote.DestinationZone)} in {quote.EtaDays} days. {SummaryCanary}");
        return JsonSerializer.Serialize(report, Json.Strict);
    }

    public IAsyncEnumerable<ChatResponseUpdate> GetStreamingResponseAsync(
        IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) =>
        throw new NotSupportedException("The scripted model is non-streaming.");

    public object? GetService(Type serviceType, object? serviceKey = null) =>
        serviceKey is not null ? null
        : serviceType == typeof(ChatClientMetadata) ? _metadata
        : serviceType.IsInstanceOfType(this) ? this
        : null;

    public void Dispose()
    {
    }
}
