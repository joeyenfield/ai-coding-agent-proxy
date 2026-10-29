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

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_console)
        findViewById<View>(android.R.id.content).padForSystemBars()
        val url = intent.getStringExtra(EXTRA_URL) ?: return finish()
        base = Uri.parse(url)
        web = findViewById(R.id.web)
        val progress = findViewById<ProgressBar>(R.id.progress)
        val offline = findViewById<View>(R.id.offline)
        findViewById<TextView>(R.id.offline_title).text = getString(R.string.offline_title, intent.getStringExtra(EXTRA_NAME) ?: base.host)
        findViewById<Button>(R.id.retry).setOnClickListener {
            offline.visibility = View.GONE
            web.reload()
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
                if (isInternalUrl(target)) return false
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
                // Keep the sign-in across restarts of the app.
                CookieManager.getInstance().flush()
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) offline.visibility = View.VISIBLE
            }
        }
        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState)
        } else {
            web.loadUrl(url + (intent.getStringExtra(EXTRA_PATH) ?: "/terminals"))
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
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

    private fun showServers() {
        startActivity(Intent(this, ServersActivity::class.java).putExtra(ServersActivity.EXTRA_STAY, true).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
        finish()
    }

    private fun isInternalUrl(target: Uri): Boolean {
        val targetHost = target.host ?: return false
        if (!targetHost.equals(base.host ?: return false, ignoreCase = true)) return false
        val sourceScheme = base.scheme?.lowercase() ?: return false
        val targetScheme = target.scheme?.lowercase() ?: return false
        val sourcePort = base.port.takeIf { it >= 0 } ?: if (sourceScheme == "https") 443 else 80
        val targetPort = target.port.takeIf { it >= 0 } ?: if (targetScheme == "https") 443 else 80
        if (sourceScheme == targetScheme) return sourcePort == targetPort
        return sourceScheme == "http" && targetScheme == "https" &&
            (sourcePort == targetPort || sourcePort == 80 && targetPort == 443)
    }

    companion object {
        const val EXTRA_URL = "url"
        const val EXTRA_NAME = "name"
        /** The console page to open, such as /get (Downloads); Running agents otherwise. */
        const val EXTRA_PATH = "path"
    }
}
