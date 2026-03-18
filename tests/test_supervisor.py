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

    def test_build_agent_state_exposes_control_metadata(self):
        payload = supervisor.build_agent_state(
            {
                "mirror_mode": "resume",
                "selected_model": "sonnet",
                "selected_effort": "high",
                "model_options": [{"id": "default"}, {"id": "sonnet"}],
                "effort_options": [{"id": "default"}, {"id": "high"}],
                "effort_matrix": {"sonnet": ["default", "high"]},
            }
        )

        self.assertEqual(payload["selected_model"], "sonnet")
        self.assertEqual(payload["selected_effort"], "high")
        self.assertEqual(payload["mirror_mode"], "resume")
        self.assertEqual(payload["effort_matrix"], {"sonnet": ["default", "high"]})

    def test_build_resume_mirror_command_prefixes_selection_args(self):
        command = supervisor.build_resume_mirror_command(
            {
                "cmd": "claude",
                "mirror_resume_args": ["--resume", "{session_id}"],
                "model_arg": ["--model", "{value}"],
                "effort_arg": ["--effort", "{value}"],
                "model_options": [{"id": "default", "value": None}, {"id": "sonnet", "value": "sonnet"}],
                "effort_options": [{"id": "default", "value": None}, {"id": "high", "value": "high"}],
                "selected_model": "sonnet",
                "selected_effort": "high",
            },
            "session-1",
        )

        self.assertIn("claude --model sonnet --effort high --resume session-1", command)


if __name__ == "__main__":
    unittest.main()
