package dev.agentproxy.app

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.Network
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView

/**
 * One server's console, full screen. It opens on Running agents; the console itself is
 * built for phones (its menu, terminal keys and input box). Sign-in is the console's own,
 * kept in the WebView's cookies. Links to other sites open in the browser.
 */
class ConsoleActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var base: Uri
    private var openingConsole = true
    private var issuer: String? = null
    private var apis: List<String> = emptyList()
    private lateinit var url: String
    private lateinit var offline: View
    /** Set by a main-frame error; the page that follows (the WebView's error page) keeps the offline view up. */
    private var failed = false
    private var network: ConnectivityManager.NetworkCallback? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_console)
        findViewById<View>(android.R.id.content).padForSystemBars()
        url = intent.getStringExtra(EXTRA_URL) ?: return finish()
        base = Uri.parse(savedInstanceState?.getString("console_base") ?: url)
        issuer = savedInstanceState?.getString("oidc_issuer")
        apis = savedInstanceState?.getStringArrayList("oidc_apis") ?: listOf(url)
        openingConsole = savedInstanceState == null
        web = findViewById(R.id.web)
        val progress = findViewById<ProgressBar>(R.id.progress)
        offline = findViewById(R.id.offline)
        findViewById<Button>(R.id.retry).setOnClickListener { retry() }
        findViewById<Button>(R.id.servers).setOnClickListener { showServers() }
        resetOfflineTitle()
        watchNetwork()

        // Debug builds can be inspected from chrome://inspect on the computer.
        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)
        CookieManager.getInstance().setAcceptCookie(true)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            // The console's own text sizes; the terminal has its own A-/A+.
            textZoom = 100
            userAgentString = "$userAgentString AgentProxyAndroid/${BuildConfig.VERSION_NAME}"
        }
        // A WebView doesn't download: hand files (the app's own APK on Downloads) to the system,
        // which saves them and offers to install. Downloads need no sign-in.
        web.setDownloadListener { downloadUrl, _, _, _, _ ->
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(downloadUrl)))
            } catch (_: ActivityNotFoundException) {
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, newProgress: Int) {
                progress.progress = newProgress
                progress.visibility = if (newProgress < 100) View.VISIBLE else View.GONE
            }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val target = request.url
                if (request.isForMainFrame && isOidcNavigation(apis, issuer, target.toString())) return false
                val launchRedirect = openingConsole && request.isForMainFrame && request.isRedirect && !request.hasGesture()
                if (isInternalConsoleUrl(base.toString(), target.toString(), launchRedirect)) {
                    if (launchRedirect) base = target
                    return false
                }
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, target))
                } catch (_: ActivityNotFoundException) {
                }
                return true
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                // An error can be reported before its error page starts; that one stays offline.
                if (!failed) offline.visibility = View.GONE
            }

            override fun onPageFinished(view: WebView, url: String) {
                openingConsole = false
                if (failed) offline.visibility = View.VISIBLE
                failed = false
                // Keep the sign-in across restarts of the app.
                CookieManager.getInstance().flush()
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) showOffline()
            }

            // A proxy or the server answering 502/503 (a restart, a tunnel down) is as offline as no
            // network, and Retry or the network coming back should load the console again.
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
                if (request.isForMainFrame && errorResponse.statusCode >= 500) showOffline()
            }
        }
        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState)
        } else {
            openConsole(url)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString("console_base", base.toString())
        outState.putString("oidc_issuer", issuer)
        outState.putStringArrayList("oidc_apis", ArrayList(apis))
        web.saveState(outState)
    }

    @Deprecated("Back goes through the console's history first")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else showServers()
    }

    override fun onPause() {
        CookieManager.getInstance().flush()
        if (::web.isInitialized) web.onPause()
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        if (::web.isInitialized) web.onResume()
    }

    override fun onDestroy() {
        network?.let { getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(it) }
        network = null
        super.onDestroy()
    }

    private fun showOffline() {
        failed = true
        openingConsole = false
        offline.visibility = View.VISIBLE
    }

    private fun resetOfflineTitle() {
        findViewById<TextView>(R.id.offline_title).text = getString(R.string.offline_title, intent.getStringExtra(EXTRA_NAME) ?: base.host)
    }

    /**
     * Load the console again: reload the page that failed once one was loaded (keeping where the
     * user was), else open it from the start, discovering the sign-in first.
     */
    private fun retry() {
        offline.visibility = View.GONE
        failed = false
        resetOfflineTitle()
        if (issuer != null && web.url != null) {
            web.reload()
        } else {
            openingConsole = true
            openConsole(url)
        }
    }

    /** Retry by itself when the network comes back while the offline view is up. */
    private fun watchNetwork() {
        val connectivity = getSystemService(ConnectivityManager::class.java) ?: return
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                runOnUiThread {
                    if (!isFinishing && !isDestroyed && offline.visibility == View.VISIBLE) retry()
                }
            }
        }
        try {
            connectivity.registerDefaultNetworkCallback(callback)
            network = callback
        } catch (_: RuntimeException) {
            // Too many callbacks registered (or no permission): Retry still works by hand.
        }
    }

    private fun openConsole(url: String) {
        Thread {
            try {
                val discovered = authIssuer(url) ?: throw java.io.IOException(getString(R.string.signin_unavailable))
                runOnUiThread {
                    if (!isFinishing && !isDestroyed) {
                        issuer = discovered.issuer
                        apis = discovered.apis
                        web.loadUrl(url + (intent.getStringExtra(EXTRA_PATH) ?: "/terminals"))
                    }
                }
            } catch (error: java.io.IOException) {
                runOnUiThread {
                    if (!isFinishing && !isDestroyed) {
                        findViewById<TextView>(R.id.offline_title).text = getString(R.string.signin_unavailable)
                        findViewById<View>(R.id.offline).visibility = View.VISIBLE
                    }
                }
            }
        }.start()
    }

    private fun showServers() {
        startActivity(Intent(this, ServersActivity::class.java).putExtra(ServersActivity.EXTRA_STAY, true).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
        finish()
    }

    companion object {
        const val EXTRA_URL = "url"
        const val EXTRA_NAME = "name"
        /** The console page to open, such as /get (Downloads); Running agents otherwise. */
        const val EXTRA_PATH = "path"
    }
}
