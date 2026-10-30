package dev.agentproxy.app

import org.json.JSONException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CarSessionDataTest {
    @Test
    fun openSessionsComeFirstWithRecentActivityFirst() {
        val sessions = CarSessionData.parse("""
            [
              {"client":"ended","ended_at":"2026-10-29","exit_status":0,"request_count":4,"last_activity_at":"2026-10-29"},
              {"client":"older","ended_at":null,"request_count":2,"last_activity_at":null,"started_at":"2026-10-27"},
              {"client":"newer","ended_at":null,"request_count":3,"last_activity_at":"2026-10-28"}
            ]
        """.trimIndent())
        assertEquals(listOf("newer", "older", "ended"), sessions.map { it.client })
        assertFalse(sessions.first().ended)
        assertTrue(sessions.last().ended)
        assertFalse(sessions.last().failed)
    }

    @Test
    fun nullableFieldsAndExitStatusAreShownWithoutExposingPaths() {
        val sessions = CarSessionData.parse("""
            [
              {"client":"codex","project":"C:\\work\\mesh","model":null,"ended_at":"now","exit_status":1,"request_count":7},
              {"client":"qwen","project":"/home/user/mesh/","model":"qwen3","ended_at":"now","exit_status":null,"request_count":0},
              {"client":"claude\n","project":null,"model":null,"ended_at":null,"request_count":1}
            ]
        """.trimIndent())
        val codex = sessions.first { it.client == "codex" }
        assertEquals("mesh", codex.project)
        assertEquals("", codex.model)
        assertTrue(codex.failed)
        assertEquals(7, codex.requests)
        val qwen = sessions.first { it.client == "qwen" }
        assertEquals("mesh", qwen.project)
        assertFalse(qwen.failed)
        assertEquals("", sessions.first { it.client == "claude" }.project)
    }

    @Test(expected = JSONException::class)
    fun invalidResponsesAreNotPresentedAsAnEmptyList() {
        CarSessionData.parse("""{"detail":"Sign in required"}""")
    }
}
