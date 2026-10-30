package dev.agentproxy.app

import java.net.URI

internal fun isOidcNavigation(api: String, issuer: String?, target: String): Boolean {
    if (issuer == null) return false
    val destination = try { URI(target) } catch (_: java.net.URISyntaxException) { return false }
    if (destination.rawUserInfo != null || destination.scheme != "https") return false
    if (isInternalConsoleUrl(issuer, target, false)) return true
    return destination.path in listOf("/api/auth/login", "/api/auth/callback") &&
        isInternalConsoleUrl(api, target, false)
}

/** A server may send the initial console navigation to its separately hosted HTTPS UI. */
internal fun isInternalConsoleUrl(source: String, target: String, launchRedirect: Boolean): Boolean {
    val base = try { URI(source) } catch (_: java.net.URISyntaxException) { return false }
    val destination = try { URI(target) } catch (_: java.net.URISyntaxException) { return false }
    val host = destination.host ?: return false
    if (destination.rawUserInfo != null) return false
    val scheme = destination.scheme?.lowercase() ?: return false
    if (launchRedirect && scheme == "https") return true
    if (!host.equals(base.host ?: return false, ignoreCase = true)) return false
    val sourceScheme = base.scheme?.lowercase() ?: return false
    val sourcePort = base.port.takeIf { it >= 0 } ?: if (sourceScheme == "https") 443 else 80
    val targetPort = destination.port.takeIf { it >= 0 } ?: if (scheme == "https") 443 else 80
    if (sourceScheme == scheme) return scheme in listOf("http", "https") && sourcePort == targetPort
    return sourceScheme == "http" && scheme == "https" &&
        (sourcePort == targetPort || sourcePort == 80 && targetPort == 443)
}
