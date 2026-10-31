package dev.agentproxy.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConsoleNavigationTest {
    @Test
    fun oidcIssuerAndApiCallbacksStayInTheAppWithoutAllowingOtherSites() {
        val api = "https://api.aicontrolmesh.com"
        val apis = listOf(api)
        val issuer = "https://auth.aicontrolmesh.com"
        assertTrue(isOidcNavigation(apis, issuer, "$api/api/auth/login"))
        assertTrue(isOidcNavigation(apis, issuer, "$api/api/auth/callback?code=one-time"))
        assertTrue(isOidcNavigation(apis, issuer, "$issuer/authorize"))
        assertFalse(isOidcNavigation(apis, null, "$issuer/authorize"))
        assertFalse(isOidcNavigation(apis, issuer, "https://evil.test/authorize"))
        assertFalse(isOidcNavigation(apis, issuer, "https://api.aicontrolmesh.com.evil.test/api/auth/callback"))
        assertFalse(isOidcNavigation(apis, issuer, "$api/unrelated"))
        assertFalse(isOidcNavigation(apis, issuer, "http://auth.aicontrolmesh.com/authorize"))
    }

    @Test
    fun splitHostingSignInStaysInTheAppOnTheApiItsCallbacksName() {
        // The app was given aicontrolmesh.com; the console it redirects to calls api.aicontrolmesh.com.
        val apis = listOf("https://aicontrolmesh.com", "https://api.aicontrolmesh.com/api/auth/callback")
        val issuer = "https://auth.aicontrolmesh.com"
        assertTrue(isOidcNavigation(apis, issuer, "https://api.aicontrolmesh.com/api/auth/login?return_to=https%3A%2F%2Fconsole.aicontrolmesh.com"))
        assertTrue(isOidcNavigation(apis, issuer, "https://api.aicontrolmesh.com/api/auth/callback?code=one-time"))
        assertFalse(isOidcNavigation(apis, issuer, "https://api.aicontrolmesh.com/docs"))
        assertFalse(isOidcNavigation(apis, issuer, "https://other.aicontrolmesh.com/api/auth/login"))
        // Without the callbacks (an older server) only the address the app was given counts.
        assertFalse(isOidcNavigation(listOf("https://aicontrolmesh.com"), issuer, "https://api.aicontrolmesh.com/api/auth/login"))
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
