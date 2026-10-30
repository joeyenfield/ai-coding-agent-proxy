package dev.agentproxy.app

import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.CookieManager
import androidx.car.app.CarAppService
import androidx.car.app.CarContext
import androidx.car.app.Screen
import androidx.car.app.Session
import androidx.car.app.model.Action
import androidx.car.app.model.ActionStrip
import androidx.car.app.model.ItemList
import androidx.car.app.model.ListTemplate
import androidx.car.app.model.MessageTemplate
import androidx.car.app.model.Row
import androidx.car.app.model.Template
import androidx.car.app.validation.HostValidator
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import org.json.JSONException
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/** Private debug build only. The release manifest and APK contain no car service. */
class CarSessionsService : CarAppService() {
    override fun createHostValidator(): HostValidator = HostValidator.Builder(this)
        // Android Auto release signing certificate from AndroidX's host allowlist.
        .addAllowedHost("com.google.android.projection.gearhead",
            "fdb00c43dbde8b51cb312aa81d3b5fa17713adb94b28f598d77f8eb89daceedf")
        .build()

    override fun onCreateSession(): Session = object : Session() {
        override fun onCreateScreen(intent: Intent): Screen = CarServersScreen(carContext)
    }
}

private class CarServersScreen(context: CarContext) : Screen(context) {
    override fun onGetTemplate(): Template {
        val servers = ServerStore(carContext).all()
        if (servers.isEmpty()) return MessageTemplate.Builder(carContext.getString(R.string.car_setup))
            .setTitle(carContext.getString(R.string.car_servers))
            .setHeaderAction(Action.APP_ICON)
            .build()
        val items = ItemList.Builder()
        servers.take(6).forEach { server ->
            items.addItem(Row.Builder().setTitle(server.name.take(64))
                .addText(server.host.take(64))
                .setOnClickListener { screenManager.push(CarSessionsScreen(carContext, server)) }
                .build())
        }
        return ListTemplate.Builder().setTitle(carContext.getString(R.string.car_servers))
            .setHeaderAction(Action.APP_ICON).setSingleList(items.build()).build()
    }
}

private class CarSessionsScreen(context: CarContext, private val server: Server) :
    Screen(context), DefaultLifecycleObserver {
    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var sessions: List<CarSessionData>? = null
    private var error: Int? = null
    private var active = false
    private var pending = false
    private var generation = 0
    private val refresh = Runnable { load() }

    init {
        lifecycle.addObserver(this)
    }

    override fun onStart(owner: LifecycleOwner) {
        active = true
        load()
    }

    override fun onStop(owner: LifecycleOwner) {
        active = false
        generation++
        sessions = null
        main.removeCallbacks(refresh)
    }

    override fun onDestroy(owner: LifecycleOwner) {
        worker.shutdownNow()
    }

    override fun onGetTemplate(): Template {
        val action = ActionStrip.Builder().addAction(Action.Builder()
            .setTitle(carContext.getString(R.string.car_refresh))
            .setOnClickListener { load() }.build()).build()
        val message = error ?: if (sessions == null) R.string.car_loading else null
        if (message != null) return MessageTemplate.Builder(carContext.getString(message))
            .setTitle(server.name.take(64)).setHeaderAction(Action.BACK)
            .setActionStrip(action).build()
        val current = sessions.orEmpty()
        val items = ItemList.Builder().setNoItemsMessage(carContext.getString(R.string.car_empty))
        if (current.isNotEmpty()) {
            val open = current.count { !it.ended }
            items.addItem(Row.Builder().setTitle(carContext.getString(R.string.car_overview, open, current.size - open)).build())
        }
        current.take(5).forEach { session ->
            val status = when {
                session.failed -> R.string.car_error
                session.ended -> R.string.car_ended
                else -> R.string.car_open
            }
            items.addItem(Row.Builder()
                .setTitle(listOf(session.client, session.project).filter { it.isNotBlank() }.joinToString(" - ").take(64))
                .addText(carContext.getString(R.string.car_session_status, carContext.getString(status), session.requests))
                .apply { if (session.model.isNotBlank()) addText(session.model.take(64)) }
                .build())
        }
        return ListTemplate.Builder().setTitle(server.name.take(64)).setHeaderAction(Action.BACK)
            .setActionStrip(action).setSingleList(items.build()).build()
    }

    private fun load() {
        if (!active || pending) return
        main.removeCallbacks(refresh)
        if (ServerStore(carContext).all().none { it.url == server.url }) {
            sessions = null
            error = R.string.car_setup
            invalidate()
            return
        }
        pending = true
        val requestGeneration = generation
        // Read cookies on the main thread and only for this exact API URL.
        val url = "${server.url}/api/sessions"
        val cookie = CookieManager.getInstance().getCookie(url)
        worker.execute {
            var result: List<CarSessionData>? = null
            var failure: Int? = null
            try {
                val connection = URL(url).openConnection() as HttpURLConnection
                try {
                    connection.connectTimeout = 4000
                    connection.readTimeout = 4000
                    // Never forward the WebView's sign-in to a redirect destination.
                    connection.instanceFollowRedirects = false
                    connection.setRequestProperty("Accept", "application/json")
                    if (cookie != null) connection.setRequestProperty("Cookie", cookie)
                    when (connection.responseCode) {
                        200 -> result = connection.inputStream.use { input ->
                            val body = ByteArrayOutputStream()
                            val buffer = ByteArray(8192)
                            while (true) {
                                val count = input.read(buffer)
                                if (count < 0) break
                                if (body.size() + count > 2_000_000) throw IOException("Session response too large")
                                body.write(buffer, 0, count)
                            }
                            CarSessionData.parse(body.toString("UTF-8"))
                        }
                        401, 403 -> failure = R.string.car_sign_in
                        else -> throw IOException("Session HTTP status ${connection.responseCode}")
                    }
                } finally {
                    connection.disconnect()
                }
            } catch (exception: IOException) {
                Log.w("CarSessions", "Session request failed", exception)
                failure = R.string.car_failed
            } catch (exception: JSONException) {
                Log.w("CarSessions", "Invalid session response", exception)
                failure = R.string.car_bad_response
            }
            main.post {
                pending = false
                if (active && generation == requestGeneration) {
                    sessions = result
                    error = failure
                    invalidate()
                    main.postDelayed(refresh, 15_000)
                } else if (active) {
                    load()
                }
            }
        }
    }
}
