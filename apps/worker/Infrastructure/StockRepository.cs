using Npgsql;

namespace Worker.Infrastructure;

public sealed class StockRepository(NpgsqlDataSource db, DemoSettings settings)
{
    // The demo delay is carried by the query itself (materialized CTE), so the Npgsql span covers it.
    // Values travel only as parameters; Npgsql tracing records the text with placeholders, never values.
    private const string Sql = """
        WITH d AS MATERIALIZED (SELECT pg_sleep(@delay))
        SELECT s.product_code, s.quantity_on_hand, s.warehouse
        FROM stock s, d
        WHERE s.product_code = @product_code
        """;

    public async Task<StockLevel> QueryAsync(ProductCode productCode, CancellationToken cancellationToken)
    {
        await using NpgsqlCommand cmd = db.CreateCommand(Sql);
        cmd.Parameters.AddWithValue("delay", settings.SqlDelay.TotalSeconds);
        cmd.Parameters.AddWithValue("product_code", Json.Wire(productCode));
        await using NpgsqlDataReader reader = await cmd.ExecuteReaderAsync(cancellationToken);
        if (!await reader.ReadAsync(cancellationToken))
        {
            throw new InvalidOperationException("stock row not found");
        }
        return new StockLevel(productCode, reader.GetInt32(1), reader.GetString(2));
    }
}
