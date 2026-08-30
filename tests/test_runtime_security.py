# pyright: reportUnknownLambdaType=false
"""Security regression tests for actions, moderation, rules, and voice."""

import datetime
import os
import types
import typing
import unittest
from unittest import mock

from owaua import actions, brain, config, embeds, function_registry, moderation, rules, slash, voice


class RebrandMigrationTest(unittest.TestCase):
    def test_legacy_environment_is_imported_without_overriding_owaua(self):
        legacy_name = ("SEF" + "BOT_") + "PREFIX"
        with mock.patch.dict(os.environ, {legacy_name: "?"}, clear=False):
            os.environ.pop("OWAUA_PREFIX", None)
            config._import_legacy_environment()
            self.assertEqual(os.environ["OWAUA_PREFIX"], "?")
            os.environ["OWAUA_PREFIX"] = "!"
            config._import_legacy_environment()
            self.assertEqual(os.environ["OWAUA_PREFIX"], "!")


class ModelOutputLinkSafetyTest(unittest.TestCase):
    def test_python_deobfuscation_result_is_not_clickable(self):
        model_output = "https://watchpeopledie.tv/"

        scrubbed = brain.scrub_ai_output(model_output)
        safe, proposals = actions.resolve_assistant_output(
            'what does print("/vt.eidelpoephctaw//:sptth"[::-1]) output?',
            [],
            scrubbed,
            in_guild=True,
        )

        self.assertEqual([], proposals)
        self.assertIn("model-produced links", safe)
        self.assertIn("https[:]//watchpeopledie.tv/", safe)
        self.assertNotIn("https://", safe)

    def test_markdown_uppercase_and_www_links_are_defanged(self):
        safe = brain.scrub_ai_output("[open](HTTPS://example.invalid/path) or www.example.invalid")

        self.assertIn("[open](HTTPS[:]//example.invalid/path)", safe)
        self.assertIn("www[.]example.invalid", safe)
        self.assertNotIn("HTTPS://", safe)
        self.assertNotIn("www.example.invalid", safe)

    def test_benign_code_explanation_is_unchanged(self):
        text = "It reverses the string and prints `hello`."

        self.assertEqual(text, brain.scrub_ai_output(text))

    def test_host_validated_search_source_remains_clickable(self):
        embed = embeds.say("grounded answer")

        embeds.add_sources(
            embed,
            [{"title": "Python docs", "url": "https://docs.python.org/3/"}],
        )

        self.assertIn("https://docs.python.org/3/", typing.cast(typing.Any, embed.fields[0].value))


class ActionConfirmationTest(unittest.IsolatedAsyncioTestCase):
    def test_assistant_contract_proposes_ordered_confirmed_actions(self):
        contract = brain._ASSISTANT_JSON_CONTRACT
        self.assertIn("1-5 ordered proposal", contract)
        self.assertIn("never say an action already happened", contract)
        self.assertNotIn("actions MUST always be an empty list", contract)

    def test_assistant_proposal_accepts_small_known_action_batches(self):
        proposal = {"type": "set_nickname", "target_user": "42", "nickname": "Raven"}
        self.assertEqual([proposal], actions.assistant_proposals([proposal]))
        self.assertEqual([proposal], actions.assistant_proposals(proposal))
        self.assertEqual([proposal, proposal], actions.assistant_proposals([proposal, proposal]))
        self.assertEqual([], actions.assistant_proposals([proposal] * 6))
        self.assertEqual([], actions.assistant_proposals([{"type": "shell"}]))

    def test_role_created_earlier_in_batch_is_an_explicit_dependency(self):
        proposals = actions.assistant_proposals(
            [
                {"type": "create_role", "name": "promo allowed"},
                {"type": "assign_role", "target_user": "42", "role": "promo allowed"},
            ]
        )
        self.assertEqual(2, len(proposals))
        self.assertEqual("promo allowed", proposals[1]["_assistant_depends_on_role_name"])

    def test_assistant_proposal_accepts_common_safe_aliases(self):
        proposal = {"type": "remove_slowmode"}
        self.assertEqual([proposal], actions.assistant_proposals([proposal]))
        self.assertEqual("set_slowmode", actions.action_type(proposal))

    def test_current_channel_action_discards_model_invented_channel_id(self):
        proposal = {
            "type": "set_slowmode",
            "channel": "976934154421829663",
            "seconds": 0,
        }
        self.assertEqual(
            {"type": "set_slowmode", "seconds": 0},
            actions.bind_assistant_channel_scope(proposal, "remove slowmode"),
        )

    def test_explicit_channel_action_keeps_only_the_users_exact_target(self):
        proposal = {
            "type": "set_slowmode",
            "channel": "123456789012345678",
            "seconds": 0,
        }
        self.assertEqual(
            proposal,
            actions.bind_assistant_channel_scope(
                proposal, "remove slowmode in <#123456789012345678>"
            ),
        )
        self.assertIsNone(
            actions.bind_assistant_channel_scope(
                proposal, "remove slowmode in <#223456789012345678>"
            )
        )

    def test_action_request_detection_does_not_match_general_creation(self):
        self.assertTrue(actions.looks_like_action_request("rename <@123456789012345678> to Raven"))
        self.assertTrue(actions.looks_like_action_request("create a private channel"))
        self.assertFalse(actions.looks_like_action_request("create a Python class for me"))
        self.assertFalse(actions.looks_like_action_request("how does slowmode work?"))
        self.assertFalse(actions.looks_like_action_request("why was that user banned?"))

    def test_clear_slowmode_request_recovers_current_channel_proposal(self):
        self.assertEqual(
            {"type": "set_slowmode", "seconds": 0},
            actions.infer_assistant_proposal("slowmode remove"),
        )
        self.assertEqual(
            {"type": "set_slowmode", "seconds": 600, "channel": "123456789012345678"},
            actions.infer_assistant_proposal("set slowmode to 10 minutes in <#123456789012345678>"),
        )

    def test_clear_action_requests_recover_without_model_action_shape(self):
        self.assertEqual(
            {"type": "purge_messages", "count": 25},
            actions.infer_assistant_proposal("purge the last 25 messages"),
        )
        self.assertEqual(
            {"type": "ban_user", "target_user": "123456789012345678"},
            actions.infer_assistant_proposal("ban <@123456789012345678>"),
        )

    def test_nickname_request_recovers_without_model_action_shape(self):
        self.assertTrue(
            actions.looks_like_action_request(
                "change <@123456789012345678>'s name to zeousky's ex"
            )
        )
        self.assertEqual(
            {
                "type": "set_nickname",
                "target_user": "123456789012345678",
                "nickname": "zeousky's ex",
            },
            actions.infer_assistant_proposal(
                "change <@123456789012345678>'s name to zeousky's ex"
            ),
        )

    def test_banned_phrase_request_recovers_a_single_batched_automod_action(self):
        request = "add brit, britt, british, UK, england as banned words"
        expected = {
            "type": "add_banned_phrases",
            "phrases": ["brit", "britt", "british", "UK", "england"],
        }
        self.assertTrue(actions.looks_like_action_request(request))
        self.assertEqual(expected, actions.infer_assistant_proposal(request))
        response, proposals = actions.resolve_assistant_output(
            request, [], "I cannot do that.", in_guild=True
        )
        self.assertEqual([expected], proposals)
        self.assertIn("Nothing has changed yet", response)

    def test_banned_phrase_parser_deduplicates_and_rejects_oversized_entries(self):
        self.assertEqual(
            ["spam", "scam"],
            actions._automod_phrases([" spam ", "SPAM", "", "x" * 61, "scam"]),
        )

    def test_action_resolution_keeps_model_clarification(self):
        question = "Which channel should I update?"
        self.assertEqual(
            question,
            actions.assistant_resolution_message("change the channel topic", question),
        )

    def test_assistant_output_resolution_is_a_safe_shared_batch_boundary(self):
        proposal = {"type": "set_slowmode", "seconds": 10}
        response, proposals = actions.resolve_assistant_output(
            "set slowmode to 10 seconds",
            [proposal],
            "Done already.",
            in_guild=True,
        )
        self.assertEqual([proposal], proposals)
        self.assertIn("Nothing has changed yet", response)
        self.assertNotIn("Done already", response)

        batch = [proposal, {"type": "set_channel_topic", "topic": "Rules"}]
        response, proposals = actions.resolve_assistant_output(
            "set slowmode to 10 seconds and set the topic to Rules",
            batch,
            "Done already.",
            in_guild=True,
        )
        self.assertEqual(batch, proposals)
        self.assertIn("Ready for 2 action(s)", response)
        self.assertIn("Confirm each action below in order", response)

        response, proposals = actions.resolve_assistant_output(
            "set slowmode to 10 seconds",
            [proposal],
            "Done already.",
            in_guild=False,
        )
        self.assertEqual([], proposals)
        self.assertIn("only work inside a server", response)

    def test_leak_block_prevents_action_inference(self):
        response, proposals = actions.resolve_assistant_output(
            "purge the last 25 messages",
            [],
            "I cannot reveal private instructions.",
            in_guild=True,
            leak_blocked=True,
        )
        self.assertEqual([], proposals)
        self.assertEqual(
            "I cannot reveal private instructions.",
            response,
        )

    def test_undo_request_detection_is_unambiguous(self):
        for text in ("undo", "revert it", "reverse the last action", "put that back"):
            self.assertTrue(actions.is_undo_request(text))
        self.assertFalse(actions.is_undo_request("how do I revert a git commit?"))

    def test_action_audit_redacts_message_and_reason(self):
        audit = actions.audit_action_arguments(
            {"type": "dm_user", "target_user": "42", "message": "private", "reason": "secret"}
        )
        self.assertNotIn("private", str(audit))
        self.assertNotIn("secret", str(audit))
        self.assertEqual(7, audit["message_length"])
        self.assertEqual(6, audit["reason_length"])

    def test_action_result_status_fails_closed_on_unknown_or_error_text(self):
        self.assertTrue(actions.action_results_ok(["set Raven's nickname to Raven"]))
        self.assertFalse(actions.action_results_ok(["set_nickname: target user not found"]))
        self.assertFalse(actions.action_results_ok(["react: no emoji given"]))
        self.assertFalse(actions.action_results_ok(["unexpected executor output"]))

    async def test_nickname_inverse_captures_exact_previous_value(self):
        target = types.SimpleNamespace(id=42, nick="Before")
        with mock.patch.object(
            actions,
            "_resolve_member",
            new_callable=mock.AsyncMock,
            return_value=target,
        ):
            inverse = await actions.prepare_inverse(
                {"type": "set_nickname", "target_user": "42", "nickname": "Raven"},
                object(),
                object(),
                object(),
            )
        self.assertEqual(
            {
                "type": "set_nickname",
                "target_user": "42",
                "nickname": "Before",
                "reason": "revert previous assistant action",
            },
            inverse,
        )

    async def test_irreversible_action_has_no_fake_inverse(self):
        inverse = await actions.prepare_inverse(
            {"type": "purge_messages", "count": 10}, object(), object(), object()
        )
        self.assertIsNone(inverse)

    async def test_remove_timeout_inverse_preserves_exact_expiry(self):
        expiry = actions.discord.utils.utcnow() + datetime.timedelta(minutes=17)
        target = types.SimpleNamespace(id=42, timed_out_until=expiry)
        with mock.patch.object(
            actions,
            "_resolve_member",
            new_callable=mock.AsyncMock,
            return_value=target,
        ):
            inverse = await actions.prepare_inverse(
                {"type": "remove_timeout", "target_user": "42"},
                object(),
                object(),
                object(),
            )
        self.assertEqual(expiry.isoformat(), typing.cast(typing.Any, inverse)["until"])

    async def test_confirmed_slowmode_supports_threads_with_manage_threads(self):
        class FakeMember:
            pass

        class FakeThread:
            id = 123456789012345678
            name = "support-thread"

            def __init__(self):
                self.edit = mock.AsyncMock()

            def permissions_for(self, _member: typing.Any):
                return types.SimpleNamespace(
                    administrator=False,
                    manage_channels=False,
                    manage_threads=True,
                )

        guild = types.SimpleNamespace(owner_id=999)
        requester = FakeMember()
        typing.cast(typing.Any, requester).id = 7
        typing.cast(typing.Any, requester).guild = guild
        typing.cast(typing.Any, requester).guild_permissions = types.SimpleNamespace(
            administrator=False
        )
        bot_member = FakeMember()
        typing.cast(typing.Any, bot_member).id = 8
        typing.cast(typing.Any, bot_member).guild = guild
        thread = FakeThread()
        guild.fetch_channel = mock.AsyncMock(return_value=thread)

        with (
            mock.patch.object(actions.discord, "Member", FakeMember),
            mock.patch.object(actions.discord, "Thread", FakeThread),
        ):
            result = await actions._one(
                {
                    "type": "set_slowmode",
                    "channel": str(thread.id),
                    "seconds": 0,
                },
                requester,
                guild,
                object(),
                channel=thread,
                bot_member=typing.cast(typing.Any, bot_member),
            )

        self.assertEqual("slowmode in #support-thread set to 0s", result)
        thread.edit.assert_awaited_once()
        self.assertEqual(
            0, typing.cast(typing.Any, thread.edit.await_args).kwargs["slowmode_delay"]
        )

    async def test_confirmed_banned_phrase_action_persists_and_syncs_automod(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(owner_id=999, id=123)
        requester = FakeMember()
        requester.id = 7
        requester.guild = guild
        requester.guild_permissions = types.SimpleNamespace(
            administrator=False, manage_guild=True
        )
        bot_member = FakeMember()
        bot_member.id = 8
        bot_member.guild = guild
        configured = {
            "enabled": False,
            "settings": {"banned_phrases": ["spam"], "delete": False},
        }

        with (
            mock.patch.object(actions.discord, "Member", FakeMember),
            mock.patch.object(actions.db, "module_config", return_value=configured),
            mock.patch.object(actions.db, "module_config_set") as persist,
            mock.patch.object(
                actions.community, "sync_native_automod", new_callable=mock.AsyncMock, return_value=True
            ) as sync,
        ):
            result = await actions._one(
                {"type": "add_banned_phrases", "phrases": ["spam", "scam", "phishing"]},
                requester,
                guild,
                object(),
                bot_member=bot_member,
            )

        self.assertEqual("added 2 blocked phrase(s) and synced Discord AutoMod", result)
        self.assertEqual(
            ["spam", "scam", "phishing"],
            persist.call_args.kwargs["settings"]["banned_phrases"],
        )
        self.assertTrue(persist.call_args.kwargs["enabled"])
        self.assertTrue(persist.call_args.kwargs["settings"]["delete"])
        sync.assert_awaited_once_with(guild)

    async def test_assistant_confirmation_records_inverse_and_consumes_undo(self):
        proposal = {"type": "set_nickname", "target_user": "42", "nickname": "Before"}
        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=7),
            guild_id=1,
            channel_id=2,
        )
        confirmation = types.SimpleNamespace(
            user=types.SimpleNamespace(id=7),
            guild=object(),
            guild_id=1,
            channel=object(),
            channel_id=2,
            client=object(),
            followup=types.SimpleNamespace(send=mock.AsyncMock()),
        )
        inverse = {"type": "set_nickname", "target_user": "42", "nickname": "Raven"}
        with (
            mock.patch.object(
                actions,
                "prepare_inverse",
                new_callable=mock.AsyncMock,
                return_value=inverse,
            ),
            mock.patch.object(
                actions,
                "execute_all",
                new_callable=mock.AsyncMock,
                return_value=["set user's nickname to Before"],
            ),
            mock.patch.object(
                actions,
                "finalize_inverse",
                new_callable=mock.AsyncMock,
                return_value=inverse,
            ),
            mock.patch.object(slash.db, "record_assistant_action") as record_history,
            mock.patch.object(slash.db, "record_action_audit"),
        ):
            view = slash._assistant_action_confirmation(
                typing.cast(typing.Any, interaction), proposal, undo_record_id=99
            )
            await view.on_confirm(typing.cast(typing.Any, confirmation))
        self.assertEqual(99, record_history.call_args.kwargs["consumed_action_id"])
        self.assertEqual(inverse, record_history.call_args.kwargs["inverse"])
        confirmation.followup.send.assert_awaited_once()

    async def test_model_actions_fail_closed_without_confirmation(self):
        proposal = {"type": "ban_user", "user_id": "42", "reason": "test"}
        with mock.patch.object(actions, "_one", new_callable=mock.AsyncMock) as execute:
            result = await actions.execute_all(
                [proposal], object(), object(), object(), confirmed=False
            )
        execute.assert_not_awaited()
        self.assertIn("confirmation required", result[0])

    async def test_one_confirmation_cannot_execute_multiple_actions(self):
        proposals = [{"type": "kick_user"}, {"type": "ban_user"}]
        with mock.patch.object(actions, "_one", new_callable=mock.AsyncMock) as execute:
            result = await actions.execute_all(
                proposals, object(), object(), object(), confirmed=True
            )
        execute.assert_not_awaited()
        self.assertIn("exactly one", result[0])

    def test_preview_canonicalizes_alias_and_suppresses_mentions(self):
        preview = actions.preview_action({"type": "ban", "target": "@everyone", "reason": "@here"})
        self.assertTrue(preview.startswith("ban_user"))
        self.assertNotIn("@everyone", preview)
        self.assertNotIn("@here", preview)

    def test_role_hierarchy_blocks_equal_rank_even_for_administrator(self):
        guild = types.SimpleNamespace(owner_id=99)
        requester = types.SimpleNamespace(id=1, guild=guild, top_role=5)
        target = types.SimpleNamespace(id=2, guild=guild, top_role=5, display_name="peer")
        self.assertIn(
            "not above",
            typing.cast(
                typing.Any,
                actions._requester_can_act_on(
                    typing.cast(typing.Any, requester), typing.cast(typing.Any, target)
                ),
            ),
        )

    def test_chart_url_accepts_only_bounded_data_schema(self):
        url = actions.chart_url(
            {
                "type": "bar",
                "labels": ["a", "b"],
                "datasets": [{"label": "values", "data": [1, 2]}],
                "plugins": {"arbitrary_callback": "ignored"},
            }
        )
        self.assertTrue(typing.cast(typing.Any, url).startswith("https://quickchart.io/chart?"))
        self.assertNotIn("arbitrary_callback", typing.cast(typing.Any, url))
        self.assertIsNone(actions.chart_url({"type": "javascript", "labels": []}))

    async def test_confirmed_action_reloads_requester_and_bot(self):
        stale_requester = types.SimpleNamespace(id=4)
        fresh_requester = types.SimpleNamespace(id=4)
        fresh_bot = types.SimpleNamespace(id=100)
        guild = types.SimpleNamespace(
            fetch_member=mock.AsyncMock(
                side_effect=typing.cast(
                    typing.Callable[..., typing.Any],
                    typing.cast(
                        typing.Callable[[typing.Any], typing.Any],
                        lambda user_id: fresh_requester if user_id == 4 else fresh_bot,
                    ),
                )
            ),
            me=fresh_bot,
        )
        client = types.SimpleNamespace(user=types.SimpleNamespace(id=100))
        with (
            mock.patch.object(actions.config, "is_blocked", return_value=False),
            mock.patch.object(
                actions, "_one", new_callable=mock.AsyncMock, return_value="done"
            ) as execute,
        ):
            result = await actions.execute_all(
                [{"type": "list_roles", "user_id": "5"}],
                stale_requester,
                guild,
                client,
                confirmed=True,
            )
        self.assertEqual(["done"], result)
        self.assertIs(typing.cast(typing.Any, execute.await_args).args[1], fresh_requester)
        self.assertIs(typing.cast(typing.Any, execute.await_args).kwargs["bot_member"], fresh_bot)

    async def test_fresh_member_resolution_never_uses_stale_cache(self):
        stale = object()
        current = object()
        guild = types.SimpleNamespace(
            get_member=mock.Mock(return_value=stale),
            fetch_member=mock.AsyncMock(return_value=current),
        )
        resolved = await actions._resolve_member(guild, "42", fresh=True)
        self.assertIs(resolved, current)
        guild.get_member.assert_not_called()


class ToolRegistryConfirmationTest(unittest.IsolatedAsyncioTestCase):
    async def test_mutating_tool_requires_confirmation_before_executor(self):
        ctx = mock.Mock()
        with mock.patch.dict(
            function_registry._EXECUTORS,
            {"ban_user": mock.AsyncMock(return_value="banned")},
            clear=False,
        ):
            result = await function_registry.execute_tool("ban_user", {"user_id": "42"}, ctx)
            typing.cast(
                mock.AsyncMock, function_registry._EXECUTORS["ban_user"]
            ).assert_not_awaited()
        self.assertIn("confirmation required", result)

    async def test_confirmed_tool_reloads_actor_before_permission_check(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(id=1, owner_id=99)
        stale_actor = FakeMember()
        typing.cast(typing.Any, stale_actor).id = 4
        fresh_actor = FakeMember()
        typing.cast(typing.Any, fresh_actor).id = typing.cast(typing.Any, stale_actor).id
        typing.cast(typing.Any, fresh_actor).guild = guild
        typing.cast(typing.Any, fresh_actor).guild_permissions = types.SimpleNamespace(
            administrator=False, ban_members=True
        )
        bot_member = FakeMember()
        typing.cast(typing.Any, bot_member).guild = guild
        typing.cast(typing.Any, bot_member).guild_permissions = types.SimpleNamespace(
            administrator=False, ban_members=True
        )
        guild.me = bot_member
        guild.fetch_member = mock.AsyncMock(return_value=fresh_actor)
        ctx = function_registry.ActionContext(
            guild=typing.cast(typing.Any, guild),
            actor=typing.cast(typing.Any, stale_actor),
            bot=typing.cast(typing.Any, types.SimpleNamespace(user=types.SimpleNamespace(id=100))),
        )
        executor = mock.AsyncMock(return_value="done")
        with (
            mock.patch.object(function_registry.discord, "Member", FakeMember),
            mock.patch.dict(function_registry._EXECUTORS, {"ban_user": executor}, clear=False),
        ):
            result = await function_registry.execute_tool(
                "ban_user", {"user_id": "5"}, ctx, confirmed=True
            )
        self.assertEqual("done", result)
        self.assertIs(typing.cast(typing.Any, executor.await_args).args[0].actor, fresh_actor)

    def test_administrator_does_not_bypass_role_hierarchy(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(owner_id=1, me=None)
        actor = FakeMember()
        typing.cast(typing.Any, actor).id = 2
        typing.cast(typing.Any, actor).top_role = 5
        typing.cast(typing.Any, actor).guild_permissions = types.SimpleNamespace(administrator=True)
        target = FakeMember()
        typing.cast(typing.Any, target).id = 3
        typing.cast(typing.Any, target).top_role = 5
        typing.cast(typing.Any, target).display_name = "peer"
        bot = types.SimpleNamespace(user=types.SimpleNamespace(id=99))
        ctx = function_registry.ActionContext(
            guild=typing.cast(typing.Any, guild),
            actor=typing.cast(typing.Any, actor),
            bot=typing.cast(typing.Any, bot),
        )
        with mock.patch.object(function_registry.discord, "Member", FakeMember):
            result = function_registry._hierarchy_ok(ctx, typing.cast(typing.Any, target))
        self.assertIn("not high enough", typing.cast(typing.Any, result))

    def test_model_can_propose_only_one_tool_call(self):
        parsed = function_registry.tool_calls_from_arguments(
            [
                {"name": "kick_user", "arguments": "{}"},
                {"name": "ban_user", "arguments": "{}"},
            ]
        )
        self.assertEqual(["kick_user"], [call["name"] for call in parsed])

    def test_audit_arguments_do_not_retain_reason_text(self):
        audit = function_registry.audit_tool_arguments(
            "timeout_user",
            {"user_id": "42", "minutes": 10, "reason": "private evidence"},
        )
        self.assertNotIn("private evidence", str(audit))
        self.assertEqual(
            {
                "tool": "timeout_user",
                "user_id": "42",
                "minutes": 10,
                "reason_supplied": True,
                "reason_length": 16,
            },
            audit,
        )

    async def test_non_object_tool_arguments_fail_closed(self):
        executor = mock.AsyncMock(return_value="unexpected")
        with mock.patch.dict(
            function_registry._EXECUTORS, {"get_server_info": executor}, clear=False
        ):
            result = await function_registry.execute_tool(
                "get_server_info", typing.cast(typing.Any, ["not", "an", "object"]), mock.Mock()
            )
        self.assertIn("must be an object", result)
        executor.assert_not_awaited()

    async def test_server_aggregates_require_current_audit_permission(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(id=1, owner_id=99)
        actor = FakeMember()
        typing.cast(typing.Any, actor).id = 4
        typing.cast(typing.Any, actor).guild = guild
        typing.cast(typing.Any, actor).guild_permissions = types.SimpleNamespace(
            administrator=False, view_audit_log=False
        )
        guild.fetch_member = mock.AsyncMock(return_value=actor)
        ctx = function_registry.ActionContext(
            guild=typing.cast(typing.Any, guild),
            actor=typing.cast(typing.Any, actor),
            bot=typing.cast(typing.Any, types.SimpleNamespace(user=types.SimpleNamespace(id=100))),
        )
        with mock.patch.object(function_registry.discord, "Member", FakeMember):
            result = await function_registry.execute_tool("get_server_info", {}, ctx)
        self.assertIn("view_audit_log", result)


class ReviewFirstModerationTest(unittest.IsolatedAsyncioTestCase):
    def test_moderation_requires_exact_boolean_guild_opt_in(self):
        for stored, expected in ((True, True), (False, False), ("true", False), (None, False)):
            with (
                self.subTest(stored=stored),
                mock.patch.object(
                    moderation.db, "guild_settings", return_value={"moderation_enabled": stored}
                ),
            ):
                self.assertIs(moderation._enabled_for_guild(1), expected)

    def test_moderation_settings_use_canonical_guild_scope(self):
        with mock.patch.object(
            moderation.db,
            "guild_settings",
            return_value={"moderation_enabled": False},
        ) as settings:
            moderation._enabled_for_guild(123)
        settings.assert_called_once_with("guild:123")

    async def test_legacy_enforce_name_only_queues_review(self):
        message = types.SimpleNamespace(delete=mock.AsyncMock())
        with mock.patch.object(moderation, "_queue_review", new_callable=mock.AsyncMock) as queue:
            await moderation._enforce(typing.cast(typing.Any, message), {"flagged": True})
        queue.assert_awaited_once()
        message.delete.assert_not_awaited()

    async def test_opted_out_guild_never_calls_classifier(self):
        message = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1),
            author=types.SimpleNamespace(id=2, bot=False),
            content="message",
            id=3,
        )
        with (
            mock.patch.object(moderation.config, "SAFETY_ENABLED", True),
            mock.patch.object(moderation.config, "SAFETY_API_KEY", "configured"),
            mock.patch.object(moderation, "_enabled_for_guild", return_value=False),
            mock.patch.object(moderation.llm, "moderate", new_callable=mock.AsyncMock) as classify,
        ):
            await moderation.safety_check(typing.cast(typing.Any, message))
        classify.assert_not_awaited()

    async def test_review_delete_reloads_current_channel_permission(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(id=1, owner_id=99)
        actor = FakeMember()
        typing.cast(typing.Any, actor).id = 4
        typing.cast(typing.Any, actor).guild = guild
        typing.cast(typing.Any, actor).guild_permissions = types.SimpleNamespace(
            administrator=False, manage_messages=True
        )
        typing.cast(typing.Any, actor).current_manage_messages = False
        bot_member = FakeMember()
        typing.cast(typing.Any, bot_member).id = 100
        typing.cast(typing.Any, bot_member).guild = guild
        typing.cast(typing.Any, bot_member).current_manage_messages = True
        source = types.SimpleNamespace(
            permissions_for=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda member: types.SimpleNamespace(
                        administrator=False,
                        manage_messages=typing.cast(typing.Any, member).current_manage_messages,
                    ),
                ),
            ),
            fetch_message=mock.AsyncMock(),
        )
        guild.get_channel = typing.cast(
            typing.Callable[..., typing.Any],
            typing.cast(typing.Callable[[typing.Any], typing.Any], lambda channel_id: source),
        )
        guild.fetch_channel = mock.AsyncMock(return_value=source)
        guild.fetch_member = mock.AsyncMock(
            side_effect=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda user_id: (
                        actor if user_id == typing.cast(typing.Any, actor).id else bot_member
                    ),
                ),
            )
        )
        interaction = types.SimpleNamespace(
            guild=guild,
            user=actor,
            client=types.SimpleNamespace(user=types.SimpleNamespace(id=100)),
            response=types.SimpleNamespace(
                send_message=mock.AsyncMock(), edit_message=mock.AsyncMock()
            ),
        )
        review = moderation.ModerationReview(
            guild_id=1,
            channel_id=10,
            message_id=11,
            author_id=12,
            category="test",
            reason="test",
            confidence=1.0,
        )
        with (
            mock.patch.object(moderation.discord, "Member", FakeMember),
            mock.patch.object(moderation, "_enabled_for_guild", return_value=True),
        ):
            view = moderation.ModerationReviewView(review, moderation.discord.Embed())
            await view._finish(typing.cast(typing.Any, interaction), delete=True)
        source.fetch_message.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertFalse(view._done)


class RulePermissionTest(unittest.TestCase):
    def test_rules_require_exact_boolean_guild_opt_in(self):
        with mock.patch.object(
            rules.db, "guild_settings", return_value={"rules_enabled": "true"}
        ) as settings:
            self.assertFalse(rules._enabled_for_guild(1))
        settings.assert_called_once_with("guild:1")

    def test_rule_approval_requires_action_specific_permission(self):
        class FakeMember:
            pass

        source = types.SimpleNamespace(
            permissions_for=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda current: types.SimpleNamespace(
                        administrator=False,
                        view_channel=True,
                        read_message_history=True,
                        manage_messages=typing.cast(
                            typing.Any, typing.cast(typing.Any, current).guild_permissions
                        ).manage_messages,
                    ),
                ),
            )
        )
        guild = types.SimpleNamespace(
            id=1,
            owner_id=99,
            get_channel=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(typing.Callable[[typing.Any], typing.Any], lambda channel_id: source),
            ),
        )
        member = FakeMember()
        typing.cast(typing.Any, member).id = 4
        typing.cast(typing.Any, member).guild = guild
        typing.cast(typing.Any, member).guild_permissions = types.SimpleNamespace(
            administrator=False,
            manage_messages=True,
            ban_members=False,
            kick_members=False,
            moderate_members=False,
        )
        pending = rules.PendingAction(
            guild_id=1,
            rule_id="test",
            rule_name="test",
            rule_detail="test",
            category="ban",
            action_label="ban",
            offender_id=5,
            offender_tag="target",
            evidence="evidence",
            channel_id=10,
            message_id=11,
            strikes=0,
            warn_limit=0,
            timeout_minutes=0,
        )
        with mock.patch.object(rules.discord, "Member", FakeMember):
            self.assertFalse(rules._can_approve(typing.cast(typing.Any, guild), member, pending))
            typing.cast(typing.Any, member).guild_permissions.ban_members = True
            self.assertTrue(rules._can_approve(typing.cast(typing.Any, guild), member, pending))
            source.permissions_for = typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda current: types.SimpleNamespace(
                        administrator=False,
                        view_channel=False,
                        read_message_history=False,
                        manage_messages=typing.cast(
                            typing.Any, typing.cast(typing.Any, current).guild_permissions
                        ).manage_messages,
                    ),
                ),
            )
            self.assertFalse(rules._can_approve(typing.cast(typing.Any, guild), member, pending))


class RuleExecutionRevalidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_permission_change_prevents_pending_ban(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(id=1, owner_id=99)
        approver = FakeMember()
        typing.cast(typing.Any, approver).id = 4
        typing.cast(typing.Any, approver).guild = guild
        typing.cast(typing.Any, approver).guild_permissions = types.SimpleNamespace(
            administrator=False,
            ban_members=False,
        )
        target = FakeMember()
        typing.cast(typing.Any, target).id = 5
        typing.cast(typing.Any, target).guild = guild
        typing.cast(typing.Any, target).ban = mock.AsyncMock()
        source = types.SimpleNamespace(
            permissions_for=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda _: types.SimpleNamespace(
                        administrator=False,
                        view_channel=True,
                        read_message_history=True,
                    ),
                ),
            )
        )
        guild.get_member = typing.cast(
            typing.Callable[..., typing.Any],
            typing.cast(
                typing.Callable[[typing.Any], typing.Any],
                lambda user_id: (
                    approver if user_id == typing.cast(typing.Any, approver).id else None
                ),
            ),
        )

        async def fetch_member(user_id: typing.Any):
            return approver if user_id == typing.cast(typing.Any, approver).id else target

        guild.fetch_member = mock.AsyncMock(side_effect=fetch_member)
        guild.fetch_channel = mock.AsyncMock(return_value=source)
        guild.me = None
        client = types.SimpleNamespace(
            get_guild=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(typing.Callable[[typing.Any], typing.Any], lambda guild_id: guild),
            ),
            user=types.SimpleNamespace(id=100),
        )
        pending = rules.PendingAction(
            guild_id=1,
            rule_id="test",
            rule_name="test",
            rule_detail="test",
            category="ban",
            action_label="ban",
            offender_id=typing.cast(typing.Any, target).id,
            offender_tag="target",
            evidence="evidence",
            channel_id=10,
            message_id=11,
            strikes=0,
            warn_limit=0,
            timeout_minutes=0,
        )
        with (
            mock.patch.object(rules.discord, "Member", FakeMember),
            mock.patch.object(rules, "_enabled_for_guild", return_value=True),
        ):
            result = await rules.execute_pending(client, pending, approver)
        self.assertIn("currently needs `ban_members`", result)
        typing.cast(typing.Any, target).ban.assert_not_awaited()

    async def test_bot_permission_is_reloaded_before_pending_ban(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(id=1, owner_id=99)
        approver = FakeMember()
        typing.cast(typing.Any, approver).id = 4
        typing.cast(typing.Any, approver).guild = guild
        typing.cast(typing.Any, approver).top_role = 10
        typing.cast(typing.Any, approver).guild_permissions = types.SimpleNamespace(
            administrator=False, ban_members=True
        )
        target = FakeMember()
        typing.cast(typing.Any, target).id = 5
        typing.cast(typing.Any, target).guild = guild
        typing.cast(typing.Any, target).top_role = 1
        typing.cast(typing.Any, target).ban = mock.AsyncMock()
        current_bot = FakeMember()
        typing.cast(typing.Any, current_bot).id = 100
        typing.cast(typing.Any, current_bot).guild = guild
        typing.cast(typing.Any, current_bot).top_role = 10
        typing.cast(typing.Any, current_bot).guild_permissions = types.SimpleNamespace(
            administrator=False, ban_members=False
        )
        guild.me = types.SimpleNamespace(
            top_role=10,
            guild_permissions=types.SimpleNamespace(administrator=True, ban_members=True),
        )
        source = types.SimpleNamespace(
            permissions_for=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda _: types.SimpleNamespace(
                        administrator=False,
                        view_channel=True,
                        read_message_history=True,
                    ),
                ),
            )
        )

        async def fetch_member(user_id: typing.Any):
            return {4: approver, 5: target, 100: current_bot}[user_id]

        guild.fetch_member = mock.AsyncMock(side_effect=fetch_member)
        guild.fetch_channel = mock.AsyncMock(return_value=source)
        client = types.SimpleNamespace(
            get_guild=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(typing.Callable[[typing.Any], typing.Any], lambda guild_id: guild),
            ),
            user=types.SimpleNamespace(id=100),
        )
        pending = rules.PendingAction(
            guild_id=1,
            rule_id="test",
            rule_name="test",
            rule_detail="test",
            category="ban",
            action_label="ban",
            offender_id=typing.cast(typing.Any, target).id,
            offender_tag="target",
            evidence="evidence",
            channel_id=10,
            message_id=11,
            strikes=0,
            warn_limit=0,
            timeout_minutes=0,
        )
        with (
            mock.patch.object(rules.discord, "Member", FakeMember),
            mock.patch.object(rules, "_enabled_for_guild", return_value=True),
        ):
            result = await rules.execute_pending(client, pending, approver)
        self.assertIn("bot needs `ban_members`", result)
        typing.cast(typing.Any, target).ban.assert_not_awaited()


class VoiceConsentTest(unittest.TestCase):
    def test_voice_settings_use_canonical_guild_scope(self):
        with mock.patch.object(
            voice.db,
            "guild_settings",
            return_value={"voice_transcription_enabled": False},
        ) as settings:
            self.assertFalse(voice._guild_stt_enabled(42))
        settings.assert_called_once_with("guild:42")

    def test_starting_stt_requires_manage_channels_not_manage_guild(self):
        class FakeMember:
            pass

        guild = types.SimpleNamespace(owner_id=99)
        member = FakeMember()
        typing.cast(typing.Any, member).id = 1
        typing.cast(typing.Any, member).guild = guild
        channel = types.SimpleNamespace(
            permissions_for=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda _: types.SimpleNamespace(
                        administrator=False, manage_channels=False, manage_guild=True
                    ),
                ),
            )
        )
        with mock.patch.object(voice.discord, "Member", FakeMember):
            self.assertFalse(voice._can_start_stt(member, channel))
            channel.permissions_for = typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda _: types.SimpleNamespace(
                        administrator=False, manage_channels=True, manage_guild=False
                    ),
                ),
            )
            self.assertTrue(voice._can_start_stt(member, channel))

    def test_session_stops_if_controller_leaves(self):
        class FakeMember:
            pass

        controller = FakeMember()
        typing.cast(typing.Any, controller).id = 1
        typing.cast(typing.Any, controller).bot = False
        voice_channel = types.SimpleNamespace(id=7, members=[])
        vc = types.SimpleNamespace(
            channel=voice_channel,
            is_connected=lambda: True,
        )
        destination = types.SimpleNamespace(
            permissions_for=typing.cast(
                typing.Callable[..., typing.Any],
                typing.cast(
                    typing.Callable[[typing.Any], typing.Any],
                    lambda _: types.SimpleNamespace(view_channel=True),
                ),
            )
        )
        session = voice.SttSession(
            10,
            destination,
            controller_id=typing.cast(typing.Any, controller).id,
            voice_channel_id=voice_channel.id,
            consenting_user_ids={typing.cast(typing.Any, controller).id},
        )
        with (
            mock.patch.object(voice.discord, "Member", FakeMember),
            mock.patch.object(voice.config, "STT_ENABLED", True),
            mock.patch.object(voice, "_guild_stt_enabled", return_value=True),
        ):
            self.assertFalse(voice._session_is_authorized(session, vc))

    def test_consent_revocation_stops_active_session(self):
        session = voice.SttSession(7, object(), consenting_user_ids={42})
        task = types.SimpleNamespace(
            done=lambda: False,
            get_loop=lambda: types.SimpleNamespace(
                call_soon_threadsafe=typing.cast(
                    typing.Callable[..., typing.Any],
                    typing.cast(
                        typing.Callable[[typing.Any], typing.Any], lambda callback: callback()
                    ),
                )
            ),
            cancel=mock.Mock(),
        )
        typing.cast(typing.Any, session).task = task
        session.voice_client = types.SimpleNamespace(stop_listening=mock.Mock())
        session.sink = types.SimpleNamespace(disarm=mock.Mock())
        voice._stt_sessions[7] = session
        try:
            with mock.patch.object(voice.db, "user_flag_set") as persist:
                voice.set_stt_consent(42, 7, False)
            persist.assert_called_once_with("42", "voice_transcription_consent:guild:7", "0")
            self.assertTrue(session.stop_event.is_set())
            task.cancel.assert_called_once_with()
            session.voice_client.stop_listening.assert_called_once_with()
            session.sink.disarm.assert_called_once_with()
        finally:
            voice._stt_sessions.pop(7, None)

    def test_stopped_session_rejects_late_audio(self):
        session = voice.SttSession(7, object(), consenting_user_ids={42})
        session.stop_event.set()
        session.enqueue(42, b"audio", 500)
        self.assertEqual(0, session.queue.qsize())


class VoiceWorkerRevocationTest(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_during_provider_call_never_posts_transcript(self):
        destination = types.SimpleNamespace(send=mock.AsyncMock())
        session = voice.SttSession(7, destination, consenting_user_ids={42})
        session.enqueue(42, b"wav", 500)
        vc = types.SimpleNamespace(stop_listening=mock.Mock())

        async def transcribe(*args: typing.Any, **kwargs: typing.Any):
            session.stop_event.set()
            return "must not be posted"

        with (
            mock.patch.object(
                voice,
                "_session_is_authorized",
                side_effect=typing.cast(
                    typing.Callable[..., typing.Any],
                    typing.cast(
                        typing.Callable[[typing.Any, typing.Any], typing.Any],
                        lambda current, current_vc: (
                            not typing.cast(
                                typing.Any, typing.cast(typing.Any, current).stop_event
                            ).is_set()
                        ),
                    ),
                ),
            ),
            mock.patch.object(voice.llm, "transcribe", side_effect=transcribe) as provider,
        ):
            await voice._stt_worker(session, vc)
        provider.assert_awaited_once()
        destination.send.assert_not_awaited()
        self.assertEqual(0, session.queue.qsize())

    async def test_recording_notice_precedes_listening(self):
        if typing.cast(typing.Any, voice).voice_recv is None:
            self.skipTest("discord-ext-voice-recv is unavailable")
        events: list[typing.Any] = []

        class FakeMember:
            pass

        class FakeRecvClient:
            def __init__(self, channel: typing.Any):
                self.channel = channel
                self.guild = None

            def is_connected(self):
                return True

            def listen(self, sink: typing.Any):
                events.append("listen")

            def stop_listening(self):
                events.append("stop")

        class FakeSink:
            def __init__(self, enqueue: typing.Any):
                self.enqueue = enqueue

            def disarm(self):
                return None

        controller = FakeMember()
        typing.cast(typing.Any, controller).id = 4
        typing.cast(typing.Any, controller).bot = False
        typing.cast(typing.Any, controller).display_name = "controller"
        bot_member = FakeMember()
        typing.cast(typing.Any, bot_member).id = 100
        typing.cast(typing.Any, bot_member).bot = True
        voice_channel = types.SimpleNamespace(id=9, name="voice", members=[controller])
        voice_channel.permissions_for = typing.cast(
            typing.Callable[..., typing.Any],
            typing.cast(
                typing.Callable[[typing.Any], typing.Any],
                lambda member: types.SimpleNamespace(
                    administrator=False,
                    manage_channels=typing.cast(typing.Any, member).id
                    == typing.cast(typing.Any, controller).id,
                ),
            ),
        )
        typing.cast(typing.Any, controller).voice = types.SimpleNamespace(channel=voice_channel)

        notice = types.SimpleNamespace()

        async def edit_notice(**kwargs: typing.Any):
            events.append("notice-edit")

        notice.edit = mock.AsyncMock(side_effect=edit_notice)
        destination = types.SimpleNamespace(id=10, name="transcripts")

        async def send_notice(*args: typing.Any, **kwargs: typing.Any):
            events.append("notice")
            return notice

        destination.send = mock.AsyncMock(side_effect=send_notice)
        destination.permissions_for = typing.cast(
            typing.Callable[..., typing.Any],
            typing.cast(
                typing.Callable[[typing.Any], typing.Any],
                lambda member: types.SimpleNamespace(
                    administrator=False,
                    view_channel=True,
                    send_messages=typing.cast(typing.Any, member).id
                    == typing.cast(typing.Any, bot_member).id,
                    send_messages_in_threads=False,
                ),
            ),
        )
        guild = types.SimpleNamespace(
            id=1,
            owner_id=99,
            me=bot_member,
            fetch_channel=mock.AsyncMock(return_value=destination),
            fetch_member=mock.AsyncMock(
                side_effect=typing.cast(
                    typing.Callable[..., typing.Any],
                    typing.cast(
                        typing.Callable[[typing.Any], typing.Any],
                        lambda user_id: (
                            controller
                            if user_id == typing.cast(typing.Any, controller).id
                            else bot_member
                        ),
                    ),
                )
            ),
        )
        destination.guild = guild
        typing.cast(typing.Any, controller).guild = guild
        typing.cast(typing.Any, bot_member).guild = guild
        vc = FakeRecvClient(voice_channel)
        typing.cast(typing.Any, vc).guild = guild
        guild.voice_client = vc
        interaction = types.SimpleNamespace(
            guild=guild,
            user=controller,
            channel=destination,
        )

        with (
            mock.patch.object(voice.discord, "Member", FakeMember),
            mock.patch.object(
                typing.cast(typing.Any, voice).voice_recv, "VoiceRecvClient", FakeRecvClient
            ),
            mock.patch.object(voice, "UtteranceSink", FakeSink),
            mock.patch.object(voice.config, "STT_ENABLED", True),
            mock.patch.object(voice.config, "GROQ_API_KEY", "configured"),
            mock.patch.object(voice, "_guild_stt_enabled", return_value=True),
        ):
            ok, _message = await voice._toggle_stt_locked(typing.cast(typing.Any, interaction))
            self.assertTrue(ok)
            self.assertLess(events.index("notice"), events.index("listen"))
            await voice._stop_stt_session(guild.id, vc)


if __name__ == "__main__":
    unittest.main()
