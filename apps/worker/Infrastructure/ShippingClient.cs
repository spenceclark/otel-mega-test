using System.Net.Http.Json;

namespace Worker.Infrastructure;

/// <summary>Typed client; the base address is fixed from DEMO_SHIPPING_URL and redirects are disabled (see Program.cs).</summary>
public sealed class ShippingClient(HttpClient http)
{
    public async Task<ShippingQuote> GetQuoteAsync(DestinationZone zone, CancellationToken cancellationToken)
    {
        using HttpResponseMessage response = await http.GetAsync($"quotes/{Json.Wire(zone)}", cancellationToken);
        response.EnsureSuccessStatusCode();
        ShippingQuote quote = await response.Content.ReadFromJsonAsync<ShippingQuote>(Json.Strict, cancellationToken)
            ?? throw new InvalidOperationException("empty shipping quote");
        return quote.DestinationZone == zone ? quote : throw new InvalidOperationException("shipping quote zone mismatch");
    }
}
