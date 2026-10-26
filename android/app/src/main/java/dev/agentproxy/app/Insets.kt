package dev.agentproxy.app

import android.os.Build
import android.view.View
import android.view.WindowInsets

/**
 * Keep a screen's content clear of the status and navigation bars and the keyboard.
 * Android 15 draws apps edge to edge, and the console (a web page) would otherwise run
 * under the bars, or size itself around them through CSS safe-area insets.
 */
fun View.padForSystemBars() {
    // Before Android 11 apps aren't edge to edge unless they ask, and these insets APIs don't exist.
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
    val start = paddingLeft
    val top = paddingTop
    val end = paddingRight
    val bottom = paddingBottom
    setOnApplyWindowInsetsListener { view, insets ->
        val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
        val keyboard = insets.getInsets(WindowInsets.Type.ime())
        view.setPadding(start + bars.left, top + bars.top, end + bars.right, bottom + maxOf(bars.bottom, keyboard.bottom))
        WindowInsets.CONSUMED
    }
    requestApplyInsets()
}
