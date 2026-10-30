package dev.agentproxy.app

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
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

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_console)
        findViewById<View>(android.R.id.content).padForSystemBars()
        val url = intent.getStringExtra(EXTRA_URL) ?: return finish()
        base = Uri.parse(savedInstanceState?.getString("console_base") ?: url)
        issuer = savedInstanceState?.getString("oidc_issuer")
        openingConsole = savedInstanceState == null
        web = findViewById(R.id.web)
        val progress = findViewById<ProgressBar>(R.id.progress)
        val offline = findViewById<View>(R.id.offline)
        findViewById<TextView>(R.id.offline_title).text = getString(R.string.offline_title, intent.getStringExtra(EXTRA_NAME) ?: base.host)
        findViewById<Button>(R.id.retry).setOnClickListener {
            offline.visibility = View.GONE
            openingConsole = true
            openConsole(url)
        }
        findViewById<Button>(R.id.servers).setOnClickListener { showServers() }

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
                if (request.isForMainFrame && isOidcNavigation(url, issuer, target.toString())) return false
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
                offline.visibility = View.GONE
            }

            override fun onPageFinished(view: WebView, url: String) {
                openingConsole = false
                // Keep the sign-in across restarts of the app.
                CookieManager.getInstance().flush()
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    openingConsole = false
                    offline.visibility = View.VISIBLE
                }
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
        web.saveState(outState)
    }

    @Deprecated("Back goes through the console's history first")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else showServers()
    }

    override fun onPause() {
        CookieManager.getInstance().flush()
        super.onPause()
    }

    private fun openConsole(url: String) {
        Thread {
            try {
                val discovered = authIssuer(url) ?: throw java.io.IOException(getString(R.string.signin_unavailable))
                runOnUiThread {
                    if (!isFinishing && !isDestroyed) {
                        issuer = discovered
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
