package dev.agentproxy.app

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.res.ColorStateList
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.webkit.CookieManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import java.util.concurrent.Executors

/** The servers this phone knows. Opening one shows its console; the last one opens by itself. */
class ServersActivity : Activity() {
    private lateinit var store: ServerStore
    private val background = Executors.newCachedThreadPool()
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_servers)
        findViewById<View>(android.R.id.content).padForSystemBars()
        store = ServerStore(this)
        findViewById<Button>(R.id.add).setOnClickListener { showForm(true) }
        findViewById<Button>(R.id.cancel).setOnClickListener { showForm(false) }
        findViewById<Button>(R.id.save).setOnClickListener { addServer() }
        // Straight back to the server used last, unless the user came back here on purpose.
        val last = store.last
        if (savedInstanceState == null && !intent.getBooleanExtra(EXTRA_STAY, false) && last != null && store.all().any { it.url == last }) {
            open(store.all().first { it.url == last })
        }
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    override fun onDestroy() {
        background.shutdownNow()
        super.onDestroy()
    }

    private fun render() {
        val list = findViewById<LinearLayout>(R.id.server_list)
        list.removeAllViews()
        val servers = store.all()
        findViewById<View>(R.id.empty).visibility = if (servers.isEmpty()) View.VISIBLE else View.GONE
        if (servers.isEmpty()) showForm(true)
        for (server in servers) {
            val item = LayoutInflater.from(this).inflate(R.layout.item_server, list, false)
            item.findViewById<TextView>(R.id.name).text = server.name
            item.findViewById<TextView>(R.id.url).text = server.url
            val state = item.findViewById<TextView>(R.id.state)
            val dot = item.findViewById<View>(R.id.dot)
            state.text = getString(R.string.checking)
            val update = item.findViewById<Button>(R.id.update)
            update.setOnClickListener { open(server, DOWNLOADS_PAGE) }
            item.setOnClickListener { open(server) }
            item.findViewById<Button>(R.id.remove).setOnClickListener { confirmRemove(server) }
            list.addView(item)
            background.execute {
                val result = check(server.url)
                main.post {
                    val (text, color) = when (result) {
                        is Check.Online -> (if (result.signIn) "Online · sign-in" else "Online") to R.color.good
                        is Check.NotAServer -> getString(R.string.not_a_server, server.host) to R.color.bad
                        is Check.Unreachable -> "Offline" to R.color.bad
                    }
                    val shown = if (result is Check.Online && result.updateAvailable) "$text · ${getString(R.string.update_available, result.version)}" else text
                    state.text = if (!server.encrypted && result is Check.Online) "$shown · ${getString(R.string.plain_http)}" else shown
                    dot.backgroundTintList = ColorStateList.valueOf(getColor(color))
                    update.visibility = if (result is Check.Online && result.updateAvailable) View.VISIBLE else View.GONE
                }
            }
        }
    }

    private fun showForm(visible: Boolean) {
        findViewById<View>(R.id.add_form).visibility = if (visible) View.VISIBLE else View.GONE
        findViewById<View>(R.id.add).visibility = if (visible) View.GONE else View.VISIBLE
        findViewById<View>(R.id.cancel).visibility = if (store.all().isEmpty()) View.GONE else View.VISIBLE
        if (visible) findViewById<EditText>(R.id.address).requestFocus()
    }

    private fun addServer() {
        val address = findViewById<EditText>(R.id.address).text.toString()
        if (address.isBlank()) return
        val url = ServerStore.normalize(address)
        val message = findViewById<TextView>(R.id.form_message)
        val save = findViewById<Button>(R.id.save)
        message.visibility = View.GONE
        save.isEnabled = false
        save.text = getString(R.string.checking)
        background.execute {
            val result = check(url)
            main.post {
                save.isEnabled = true
                save.text = getString(R.string.add)
                val host = android.net.Uri.parse(url).host ?: url
                when (result) {
                    is Check.Online -> {
                        val name = findViewById<EditText>(R.id.name).text.toString().trim().ifEmpty { host }
                        store.add(Server(name, url))
                        findViewById<EditText>(R.id.address).text.clear()
                        findViewById<EditText>(R.id.name).text.clear()
                        showForm(false)
                        render()
                    }
                    is Check.NotAServer -> {
                        message.text = getString(R.string.not_a_server, host)
                        message.visibility = View.VISIBLE
                    }
                    is Check.Unreachable -> {
                        message.text = getString(R.string.unreachable, host, result.reason)
                        message.visibility = View.VISIBLE
                    }
                }
            }
        }
    }

    private fun confirmRemove(server: Server) {
        AlertDialog.Builder(this)
            .setMessage(getString(R.string.remove_server, server.name))
            .setPositiveButton(R.string.remove) { _, _ ->
                store.remove(server)
                forgetSignIn(server)
                render()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    /** Expire the console's cookies for that server, so the next person to add it signs in again. */
    private fun forgetSignIn(server: Server) {
        val cookies = CookieManager.getInstance()
        for (name in listOf("ai_proxy_session", "ai_proxy_remote", "mesh_oidc_state")) {
            cookies.setCookie(server.url, "$name=; Max-Age=0; Path=/")
        }
        cookies.setCookie(server.url + "/api/auth", "mesh_refresh=; Max-Age=0; Path=/api/auth")
        cookies.flush()
    }

    private fun open(server: Server, path: String? = null) {
        store.last = server.url
        val intent = Intent(this, ConsoleActivity::class.java).putExtra(ConsoleActivity.EXTRA_URL, server.url).putExtra(ConsoleActivity.EXTRA_NAME, server.name)
        if (path != null) intent.putExtra(ConsoleActivity.EXTRA_PATH, path)
        startActivity(intent)
    }

    companion object {
        const val EXTRA_STAY = "stay"
        /** The console's Downloads page, with the app built for that server (/downloads itself is the files). */
        const val DOWNLOADS_PAGE = "/get"
    }
}
