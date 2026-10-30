package dev.agentproxy.app

import org.json.JSONArray
import org.json.JSONObject

/** Only metadata is projected: never session credentials, prompts or terminal output. */
internal data class CarSessionData(
    val client: String,
    val project: String,
    val model: String,
    val ended: Boolean,
    val failed: Boolean,
    val requests: Int,
    val activity: String,
) {
    companion object {
        fun parse(body: String): List<CarSessionData> {
            val array = JSONArray(body)
            return (0 until array.length()).map { index ->
                val item = array.getJSONObject(index)
                val ended = !item.isNull("ended_at")
                CarSessionData(
                    client = text(item, "client").take(160).ifBlank { "Agent" },
                    project = text(item, "project").trimEnd('/', '\\').substringAfterLast('/').substringAfterLast('\\').take(160),
                    model = text(item, "model").take(160),
                    ended = ended,
                    failed = ended && !item.isNull("exit_status") && item.getInt("exit_status") != 0,
                    requests = item.getInt("request_count").coerceAtLeast(0),
                    activity = text(item, "last_activity_at").ifBlank { text(item, "started_at") },
                )
            }.sortedWith(compareBy<CarSessionData> { it.ended }.thenByDescending { it.activity })
        }

        private fun text(item: JSONObject, key: String): String =
            if (item.isNull(key)) "" else item.getString(key).filterNot { it.isISOControl() }
    }
}
