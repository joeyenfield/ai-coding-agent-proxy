package dev.agentproxy.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConsoleNavigationTest {
    @Test
    fun oidcIssuerAndApiCallbacksStayInTheAppWithoutAllowingOtherSites() {
        val api = "https://api.aicontrolmesh.com"
        val issuer = "https://auth.aicontrolmesh.com"
        assertTrue(isOidcNavigation(api, issuer, "$api/api/auth/login"))
        assertTrue(isOidcNavigation(api, issuer, "$api/api/auth/callback?code=one-time"))
        assertTrue(isOidcNavigation(api, issuer, "$issuer/authorize"))
        assertFalse(isOidcNavigation(api, null, "$issuer/authorize"))
        assertFalse(isOidcNavigation(api, issuer, "https://evil.test/authorize"))
        assertFalse(isOidcNavigation(api, issuer, "https://api.aicontrolmesh.com.evil.test/api/auth/callback"))
        assertFalse(isOidcNavigation(api, issuer, "$api/unrelated"))
        assertFalse(isOidcNavigation(api, issuer, "http://auth.aicontrolmesh.com/authorize"))
    }

    @Test
    fun initialHttpsConsoleRedirectStaysInApp() {
        assertTrue(isInternalConsoleUrl("https://aicontrolmesh.com", "https://console.aicontrolmesh.com/terminals", true))
        assertFalse(isInternalConsoleUrl("https://aicontrolmesh.com", "https://console.aicontrolmesh.com/terminals", false))
        assertTrue(isInternalConsoleUrl("https://console.aicontrolmesh.com/terminals", "https://console.aicontrolmesh.com/sessions", false))
    }

    @Test
    fun redirectsCannotDowngradeOrOpenNonWebSchemes() {
        assertFalse(isInternalConsoleUrl("https://example.com", "http://console.example.com", true))
        assertFalse(isInternalConsoleUrl("https://example.com", "intent://console.example.com", true))
        assertFalse(isInternalConsoleUrl("https://example.com", "https://user@console.example.com", true))
    }

    @Test
    fun existingOriginAndUpgradeRulesRemain() {
        assertTrue(isInternalConsoleUrl("http://server", "https://server/terminals", false))
        assertTrue(isInternalConsoleUrl("http://server:9999", "https://server:9999/terminals", false))
        assertTrue(isInternalConsoleUrl("https://SERVER:443", "https://server/terminals", false))
        assertFalse(isInternalConsoleUrl("https://server", "https://server:9999", false))
        assertFalse(isInternalConsoleUrl("https://server", "https://server.evil.example", false))
        assertFalse(isInternalConsoleUrl("https://server", "http://server", false))
    }
}
