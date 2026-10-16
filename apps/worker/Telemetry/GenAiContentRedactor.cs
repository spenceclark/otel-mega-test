using System.Diagnostics;
using System.Text.Json.Nodes;
using OpenTelemetry;

namespace Worker.Telemetry;

/// <summary>
/// "Content capture off" as redaction rather than omission, matching what Pydantic AI exports: MAF / M.E.AI record
/// messages (EnableSensitiveData = true) and this processor reduces them to their shape before any exporter sees the
/// span. Roles, part types, tool-call ids and names, and finish reasons stay; text, tool arguments, tool results and
/// system instructions go. Registered ahead of the OTLP exporter, and only when DEMO_CONTENT_CAPTURE is false, so
/// content never leaves the process.
/// </summary>
public sealed class GenAiContentRedactor : BaseProcessor<Activity>
{
    private static readonly string[] _dropped = ["gen_ai.system_instructions", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"];
    private static readonly string[] _messages = ["gen_ai.input.messages", "gen_ai.output.messages"];
    private static readonly HashSet<string> _keptPartFields = ["type", "id", "name"];

    public override void OnEnd(Activity data)
    {
        foreach (string key in _dropped)
        {
            if (data.GetTagItem(key) is not null)
            {
                data.SetTag(key, null);
            }
        }
        foreach (string key in _messages)
        {
            if (data.GetTagItem(key) is not null)
            {
                data.SetTag(key, data.GetTagItem(key) is string json ? Shape(json) : null);
            }
        }
    }

    /// <summary>The message list without content, or null (attribute removed) if it is not the expected JSON.</summary>
    internal static string? Shape(string json)
    {
        try
        {
            var shaped = new JsonArray();
            foreach (JsonNode? message in JsonNode.Parse(json)!.AsArray())
            {
                var parts = new JsonArray();
                foreach (JsonNode? part in message!["parts"]?.AsArray() ?? [])
                {
                    var kept = new JsonObject();
                    foreach ((string field, JsonNode? value) in part!.AsObject())
                    {
                        if (_keptPartFields.Contains(field))
                        {
                            kept[field] = value?.DeepClone();
                        }
                    }
                    parts.Add(kept);
                }
                var shapedMessage = new JsonObject { ["role"] = message["role"]?.DeepClone(), ["parts"] = parts };
                if (message["finish_reason"] is JsonNode finishReason)
                {
                    shapedMessage["finish_reason"] = finishReason.DeepClone();
                }
                shaped.Add(shapedMessage);
            }
            return shaped.ToJsonString();
        }
        catch (Exception) // fail closed: an unexpected shape is dropped, never exported as-is
        {
            return null;
        }
    }
}
