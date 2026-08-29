import time
import typing
import unittest

from owaua import actions, config, db, rules, staffops


class StaffOperationsTests(unittest.TestCase):
    def setUp(self):
        db.close()
        self.previous_db = config.DB_PATH
        config.DB_PATH = ":memory:"

    def tearDown(self):
        db.close()
        config.DB_PATH = self.previous_db

    def test_case_search_notes_appeals_and_timeline_are_guild_scoped(self):
        case = staffops.create_case(
            "guild:123",
            actor_id="staff:1",
            subject_id="42",
            category="automod bypass",
            reason="Repeated separator bypass attempts",
            severity="high",
            evidence_links=["https://discord.com/channels/123/20/30#fragment"],
            expires_at=time.time() + 86_400,
        )
        self.assertEqual("CASE-000001", case["case_number"])
        self.assertEqual(["https://discord.com/channels/123/20/30"], case["evidence_links"])
        self.assertEqual([], staffops.search_cases("guild:999", query="separator"))
        self.assertEqual(case["id"], staffops.search_cases("guild:123", query="separator")[0]["id"])

        staffops.add_member_note(
            "guild:123",
            actor_id="staff:2",
            subject_id="42",
            note="Keep future actions review-only.",
            case_id=case["id"],
        )
        appealed = staffops.open_appeal(
            "guild:123",
            case["id"],
            appellant_id="42",
            statement="The matched text was quoted for context.",
        )
        self.assertEqual("appealed", appealed["status"])
        self.assertEqual("pending", appealed["appeal_status"])
        self.assertEqual(
            {"event", "note", "appeal"},
            {entry["kind"] for entry in appealed["timeline"]},
        )
        resolved = staffops.update_case(
            "guild:123",
            case["id"],
            actor_id="staff:2",
            status="resolved",
            appeal_status="accepted",
            assigned_to="55",
        )
        self.assertEqual("resolved", resolved["status"])
        self.assertEqual("accepted", resolved["appeal_status"])

    def test_case_evidence_and_cross_subject_appeals_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            staffops.create_case(
                "guild:123",
                actor_id="staff",
                subject_id="42",
                category="test",
                reason="test",
                evidence_links=["http://example.test/evidence"],
            )
        case = staffops.create_case(
            "guild:123",
            actor_id="staff",
            subject_id="42",
            category="test",
            reason="test",
        )
        with self.assertRaisesRegex(ValueError, "case subject"):
            staffops.open_appeal("guild:123", case["id"], appellant_id="43", statement="not mine")

    def test_incident_center_health_retention_and_csv_are_bounded_aggregates(self):
        incident = staffops.record_incident(
            "guild:123",
            source="malware",
            summary="Attachment blocked",
            severity="critical",
            subject_id="42",
            reference="channel:20/message:30",
        )
        staffops.update_incident(
            "guild:123",
            incident["id"],
            actor_id="staff:1",
            status="escalated",
            assigned_to="55",
        )
        rows = staffops.incident_center(
            "guild:123", source="malware", status="escalated", assigned_to="55"
        )
        self.assertEqual(incident["id"], rows[0]["id"])
        health = staffops.server_health("guild:123")
        self.assertTrue(health["advisory_only"])
        self.assertTrue(all(not item["automatic_change"] for item in health["recommendations"]))
        inventory = staffops.retention_inventory("guild:123")
        self.assertTrue(any(item["module"] == "Moderation cases" for item in inventory["modules"]))
        exported = staffops.analytics_csv("guild:123")
        self.assertIn("scope,metric,value,generated_at", exported)
        self.assertNotIn("Attachment blocked", exported)


class AssistantPlanAndBypassTests(unittest.TestCase):
    def test_plan_preview_is_non_executing_and_one_mutation_per_step(self):
        raw = [
            {
                "title": "Inspect current slowmode",
                "explanation": "Read the current channel configuration.",
                "permission": "view_channel",
                "mutation": False,
                "action": None,
            },
            {
                "title": "Set slowmode",
                "explanation": "Apply ten seconds after a separate confirmation.",
                "permission": "",
                "mutation": True,
                "action": {"type": "set_slowmode", "seconds": 10},
            },
        ]
        response, proposals = actions.resolve_assistant_output(
            "plan how you would clean up this channel",
            [],
            "done",
            in_guild=True,
            raw_plan=raw,
        )
        self.assertEqual([], proposals)
        self.assertIn("nothing has changed", response)
        self.assertIn("separate confirmation required", response)
        self.assertIn("manage_channels", response)
        self.assertEqual(
            [],
            actions.assistant_plan(
                [{"title": "Hidden mutation", "mutation": True, "action": None}]
            ),
        )

    def test_common_unicode_leet_separator_and_repeat_bypasses_normalize(self):
        self.assertEqual("kys", rules.normalize_for_rules("k\u200by\u200bs"))
        self.assertEqual("kys", rules.normalize_for_rules("k.y.s"))
        self.assertEqual("pedophile", typing.cast(typing.Any, rules.match_rule("p3d0phiiiile")).id)
        self.assertEqual("kys", typing.cast(typing.Any, rules.match_rule("k.y.s")).id)
        self.assertIsNone(rules.match_rule("classic assignment cockpit"))
