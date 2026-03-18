import unittest

import supervisor


class SupervisorTests(unittest.TestCase):
    def test_parse_transcript_entries_groups_tagged_blocks(self):
        text = "[FARHAN]\nhello\n\n[CODEX]\nhi there\n"

        entries = supervisor.parse_transcript_entries(text, limit=10)

        self.assertEqual(
            entries,
            [
                {"speaker": "FARHAN", "text": "hello"},
                {"speaker": "CODEX", "text": "hi there"},
            ],
        )

    def test_desired_mirror_view_falls_back_to_log_without_session(self):
        agent = {
            "mirror_mode": "resume",
            "mirror_resume_args": ["resume", "{session_id}"],
        }

        self.assertEqual(supervisor.desired_mirror_view(agent, None), "log")
        self.assertEqual(supervisor.desired_mirror_view(agent, "abc"), "resume")

    def test_infer_agent_state_preserves_error(self):
        self.assertEqual(
            supervisor.infer_agent_state("error", "running", "resume", "codex"),
            "error",
        )

    def test_infer_agent_state_moves_ready_when_relay_running_and_pane_alive(self):
        self.assertEqual(
            supervisor.infer_agent_state("auth", "running", "log", "tail"),
            "ready",
        )


if __name__ == "__main__":
    unittest.main()
