package dev.agentproxy.app

import android.content.Context
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** An agent-server this phone knows: what to call it and where it is. */
data class Server(val name: String, val url: String) {
    val host: String get() = Uri.parse(url).host ?: url
    val encrypted: Boolean get() = url.startsWith("https://")
}

/** The servers, kept in the app's own preferences (the sign-in itself is the WebView's cookie). */
class ServerStore(context: Context) {
    private val preferences = context.getSharedPreferences("servers", Context.MODE_PRIVATE)

    fun all(): List<Server> {
        val array = JSONArray(preferences.getString("servers", "[]"))
        return (0 until array.length()).map { index ->
            val item = array.getJSONObject(index)
            Server(item.getString("name"), item.getString("url"))
        }
    }

    fun add(server: Server) = save(all().filter { it.url != server.url } + server)

    fun remove(server: Server) {
        save(all().filter { it.url != server.url })
        if (last == server.url) last = null
    }

    /** The server opened last, which the app goes straight back to. */
    var last: String?
        get() = preferences.getString("last", null)
        set(value) = preferences.edit().putString("last", value).apply()

    private fun save(servers: List<Server>) {
        val array = JSONArray()
        servers.forEach { array.put(JSONObject().put("name", it.name).put("url", it.url)) }
        preferences.edit().putString("servers", array.toString()).apply()
    }

    companion object {
        /** "server:9999" or "http://server:9999/" as the server's base URL. */
        fun normalize(input: String): String {
            var url = input.trim().trimEnd('/')
            if (!url.contains("://")) url = "http://$url"
            return url
        }
    }
}

/** How a server answers: whether it's an agent-server at all, whether it wants a sign-in, and its version. */
sealed class Check {
    data class Online(val signIn: Boolean, val version: String?) : Check() {
        /** The server is another version than this app: its Downloads page has the app built for it. */
        val updateAvailable: Boolean get() = version != null && version != BuildConfig.VERSION_NAME
    }
    data class NotAServer(val status: Int) : Check()
    data class Unreachable(val reason: String) : Check()
}

/** Asks the server's /health and /api/auth/status. Call it off the main thread. */
fun check(url: String): Check {
    return try {
        val health = get("$url/health")
        if (health.first != 200 || !health.second.has("proxy_instance_id")) return Check.NotAServer(health.first)
        val auth = get("$url/api/auth/status").second
        // Servers from before versions were negotiated don't say theirs.
        val version = health.second.optString("version").takeIf { it.isNotEmpty() }
        Check.Online(auth.optBoolean("required", false), version)
    } catch (error: Exception) {
        Check.Unreachable(error.message ?: error.javaClass.simpleName)
    }
}

/** Only the chosen API may name the issuer whose sign-in pages stay in the WebView. */
fun authIssuer(url: String): String? {
    val response = get("$url/api/auth/status")
    if (response.first != 200) return null
    val issuer = response.second.optString("issuer")
    val parsed = URL(issuer)
    if (parsed.protocol != "https" || parsed.userInfo != null || parsed.query != null || parsed.ref != null) return null
    return issuer
}

private fun get(url: String): Pair<Int, JSONObject> {
    val connection = URL(url).openConnection() as HttpURLConnection
    connection.instanceFollowRedirects = false
    connection.connectTimeout = 4000
    connection.readTimeout = 4000
    try {
        val status = connection.responseCode
        val body = (if (status < 400) connection.inputStream else connection.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
        return status to (runCatching { JSONObject(body) }.getOrNull() ?: JSONObject())
    } finally {
        connection.disconnect()
    }
}
