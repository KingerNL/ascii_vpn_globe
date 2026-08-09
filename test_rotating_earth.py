import math
import unittest
from unittest import mock

import rotating_earth as earth


class TextureTests(unittest.TestCase):
    def test_known_land_and_ocean_points(self) -> None:
        points = {
            "New York": (-74.0, 40.7, True),
            "Sahara": (13.0, 23.0, True),
            "Australia": (133.0, -25.0, True),
            "Pacific": (-140.0, 0.0, False),
            "South Atlantic": (-20.0, -30.0, False),
            "Antarctica": (0.0, -80.0, True),
        }
        for name, (longitude, latitude, expected) in points.items():
            with self.subTest(name=name):
                self.assertEqual(
                    earth.is_land(
                        math.radians(longitude),
                        math.radians(latitude),
                    ),
                    expected,
                )

    def test_terrain_texture_is_precomputed(self) -> None:
        self.assertEqual(
            len(earth._TERRAIN_TEXTURE),
            earth.TEXTURE_WIDTH * earth.TEXTURE_HEIGHT,
        )
        self.assertGreater(min(earth._TERRAIN_TEXTURE), 0.0)
        self.assertLess(max(earth._TERRAIN_TEXTURE), 1.0)
        with mock.patch.object(
            earth.math,
            "sin",
            side_effect=AssertionError("terrain must use its lookup texture"),
        ):
            earth._surface_sample(0.0, 0.0, 1.0, 0.0, 0.0)


class ProjectionTests(unittest.TestCase):
    def test_center_tracks_yaw_and_pitch(self) -> None:
        yaw = math.radians(35.0)
        pitch = math.radians(20.0)
        projected = earth.screen_to_geo(0.0, 0.0, yaw, pitch)
        self.assertIsNotNone(projected)
        longitude, latitude, depth = projected
        self.assertAlmostEqual(longitude, yaw)
        self.assertAlmostEqual(latitude, pitch)
        self.assertAlmostEqual(depth, 1.0)

    def test_point_outside_globe_is_rejected(self) -> None:
        self.assertIsNone(earth.screen_to_geo(1.0, 1.0, 0.0, 0.0))

    def test_geo_projection_reverses_screen_projection(self) -> None:
        yaw = math.radians(25.0)
        pitch = math.radians(35.0)
        screen_x = 0.2
        screen_y = -0.3
        projected = earth.screen_to_geo(screen_x, screen_y, yaw, pitch)
        self.assertIsNotNone(projected)
        longitude, latitude, expected_depth = projected
        actual_x, actual_y, actual_depth = earth.geo_to_screen(
            longitude, latitude, yaw, pitch
        )
        self.assertAlmostEqual(actual_x, screen_x)
        self.assertAlmostEqual(actual_y, screen_y)
        self.assertAlmostEqual(actual_depth, expected_depth)

    def test_render_caches_frame_rotation_trigonometry(self) -> None:
        original_sin = math.sin
        original_cos = math.cos
        with (
            mock.patch.object(earth.math, "sin", wraps=original_sin) as sine,
            mock.patch.object(earth.math, "cos", wraps=original_cos) as cosine,
        ):
            earth.render_globe(
                48,
                16,
                earth.DEFAULT_YAW,
                earth.DEFAULT_PITCH,
                color=False,
                show_cities=False,
            )
        self.assertLessEqual(sine.call_count, 4)
        self.assertLessEqual(cosine.call_count, 4)


class NordVpnCityTests(unittest.TestCase):
    def test_bundled_city_data_is_complete_and_unique(self) -> None:
        identifiers = [city[0] for city in earth.NORDVPN_CITIES]
        self.assertEqual(len(identifiers), 224)
        self.assertEqual(len(set(identifiers)), 224)
        self.assertIn("gb_london", identifiers)

    def test_default_view_projects_city_markers(self) -> None:
        markers = earth.project_city_markers(
            80,
            23,
            earth.DEFAULT_YAW,
            earth.DEFAULT_PITCH,
        )
        self.assertGreater(len(markers), 100)
        self.assertTrue(
            all(
                0 <= x < 80 and 0 <= y < 23 and 0 < mask <= 0xFF
                for (x, y), mask in markers.items()
            )
        )

    def test_city_can_be_picked_and_rendered_with_a_label(self) -> None:
        identifier, _, _, longitude, latitude = next(
            city for city in earth.NORDVPN_CITY_DATA if city[0] == "de_berlin"
        )
        projected = next(
            city
            for city in earth.project_cities(80, 23, longitude, latitude)
            if city.identifier == identifier
        )
        picked = earth.pick_city(
            projected.cell[0],
            projected.cell[1],
            80,
            23,
            longitude,
            latitude,
        )
        self.assertIsNotNone(picked)
        self.assertEqual(picked.identifier, "de_berlin")

        rows = earth.render_globe(
            80,
            23,
            longitude,
            latitude,
            color=False,
            show_cities=True,
            selected_city=picked.identifier,
        )
        self.assertIn("[Berlin, DE]", "\n".join(rows))

    def test_city_color_can_be_disabled(self) -> None:
        with_cities = "".join(
            earth.render_globe(
                48,
                16,
                earth.DEFAULT_YAW,
                earth.DEFAULT_PITCH,
                color=True,
                show_cities=True,
            )
        )
        without_cities = "".join(
            earth.render_globe(
                48,
                16,
                earth.DEFAULT_YAW,
                earth.DEFAULT_PITCH,
                color=True,
                show_cities=False,
            )
        )
        self.assertIn(earth.CITY_COLOR, with_cities)
        self.assertNotIn(earth.CITY_COLOR, without_cities)

    def test_hovered_city_uses_requested_color(self) -> None:
        frame = "".join(
            earth.render_globe(
                80,
                23,
                earth.DEFAULT_YAW,
                earth.DEFAULT_PITCH,
                color=True,
                show_cities=True,
                hovered_city="de_berlin",
            )
        )
        self.assertEqual(earth.HOVER_CITY_COLOR, "\x1b[38;2;236;73;53m")
        self.assertIn(earth.HOVER_CITY_COLOR, frame)

    def test_hovered_label_uses_requested_color(self) -> None:
        frame = "".join(
            earth.render_globe(
                80,
                23,
                earth.DEFAULT_YAW,
                earth.DEFAULT_PITCH,
                color=True,
                show_cities=True,
                selected_city="de_berlin",
                hovered_label=True,
            )
        )
        self.assertIn(earth.HOVER_CITY_COLOR, frame)


class NordVpnConnectionTests(unittest.TestCase):
    def test_confirmation_and_result_popups_render(self) -> None:
        confirmation = "\n".join(
            earth.render_globe(
                80,
                23,
                earth.DEFAULT_YAW,
                earth.DEFAULT_PITCH,
                color=False,
                confirm_city="de_berlin",
            )
        )
        self.assertIn("Connect to Berlin, DE?", confirmation)
        self.assertIn("y/Enter: connect", confirmation)

        result = "\n".join(
            earth.render_globe(
                80,
                23,
                earth.DEFAULT_YAW,
                earth.DEFAULT_PITCH,
                color=False,
                dialog_message="Connected to Berlin.",
            )
        )
        self.assertIn("Connected to Berlin.", result)

    def test_connect_uses_country_code_and_city_without_a_shell(self) -> None:
        completed = mock.Mock(
            stdout=(
                "A new version of NordVPN is available!\n"
                "Please update the app.\n"
                "You are connected to Berlin.\n"
            ),
            stderr="",
            returncode=0,
        )
        with mock.patch.object(
            earth.subprocess,
            "run",
            return_value=completed,
        ) as run:
            message = earth.connect_nordvpn("de_berlin")

        self.assertEqual(message, "You are connected to Berlin.")
        run.assert_called_once_with(
            ["nordvpn", "connect", "de", "Berlin"],
            capture_output=True,
            text=True,
            timeout=45.0,
            check=False,
        )


class BrailleRendererTests(unittest.TestCase):
    def test_character_table_contains_the_full_braille_block(self) -> None:
        self.assertEqual(len(earth.BRAILLE), 256)
        self.assertEqual(earth.BRAILLE[0], "\u2800")
        self.assertEqual(earth.BRAILLE[-1], "\u28ff")
        self.assertEqual(
            [ord(character) for character in earth.BRAILLE],
            list(range(0x2800, 0x2900)),
        )

    def test_braille_dot_layout(self) -> None:
        self.assertEqual(earth.BRAILLE_BITS[0][0], 0x01)
        self.assertEqual(earth.BRAILLE_BITS[3][0], 0x40)
        self.assertEqual(earth.BRAILLE_BITS[0][1], 0x08)
        self.assertEqual(earth.BRAILLE_BITS[3][1], 0x80)

    def test_monochrome_frame_has_requested_dimensions(self) -> None:
        rows = earth.render_globe(48, 16, 0.0, 0.0, color=False)
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(len(row) == 48 for row in rows))
        visible = [character for row in rows for character in row if character != " "]
        self.assertTrue(visible)
        self.assertTrue(all(0x2800 <= ord(character) <= 0x28FF for character in visible))

    def test_original_sparse_density_is_restored(self) -> None:
        rows = earth.render_globe(
            80,
            23,
            earth.DEFAULT_YAW,
            earth.DEFAULT_PITCH,
            color=False,
            show_cities=False,
        )
        full_cells = "".join(rows).count("\u28ff")
        self.assertGreater(full_cells, 5)
        self.assertLess(full_cells, 20)

class BoundaryTests(unittest.TestCase):
    def test_requested_boundary_data_is_bundled(self) -> None:
        labels = [label for label, _ in earth.BORDER_LINES]
        self.assertEqual(len(labels), 12)
        self.assertEqual(sum(len(points) for _, points in earth.BORDER_LINES), 179)
        self.assertIn("United States-Canada (mainland)", labels)
        self.assertIn("United States-Canada (Alaska)", labels)
        self.assertIn("Russia-Finland", labels)
        self.assertIn("Russia-Ukraine", labels)

    def test_internal_borders_project_in_america_and_europe(self) -> None:
        views = (
            (math.radians(-100.0), math.radians(50.0)),
            (math.radians(30.0), math.radians(55.0)),
        )
        for yaw, pitch in views:
            with self.subTest(yaw=yaw):
                markers = earth.project_internal_borders(100, 30, yaw, pitch)
                self.assertGreater(len(markers), 30)
                self.assertTrue(all(0 < mask <= 0xFF for mask in markers.values()))

        frame = "".join(
            earth.render_globe(
                100,
                30,
                views[0][0],
                views[0][1],
                color=True,
                show_cities=False,
            )
        )
        self.assertIn(earth.INTERNAL_BORDER_COLOR, frame)


class ParallelRendererTests(unittest.TestCase):
    def test_row_ranges_cover_frame_without_gaps(self) -> None:
        ranges = earth._split_row_ranges(80, 23, 4)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 23)
        self.assertEqual(len(ranges), 4)
        self.assertTrue(
            all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
        )

    def test_independent_strips_equal_single_process_frame(self) -> None:
        arguments = (
            80,
            23,
            earth.DEFAULT_YAW,
            earth.DEFAULT_PITCH,
            True,
            True,
            "de_berlin",
            None,
            False,
            None,
            None,
        )
        expected = earth.render_globe(*arguments)
        actual = [
            line
            for row_start, row_end in earth._split_row_ranges(80, 23, 4)
            for line in earth.render_globe(
                *arguments,
                row_start=row_start,
                row_end=row_end,
            )
        ]
        self.assertEqual(actual, expected)

    def test_process_pool_frame_matches_single_process(self) -> None:
        state = earth.GlobeState(
            selected_city="de_berlin",
            hovered_label=True,
        )
        expected = earth.render_globe(
            64,
            18,
            state.yaw,
            state.pitch,
            True,
            state.show_cities,
            state.selected_city,
            state.hovered_city,
            state.hovered_label,
        )
        renderer = earth.ParallelRenderer(2)
        with renderer:
            self.assertIsNotNone(renderer.executor)
            actual = renderer.render(state, 64, 18, True)
        self.assertEqual(actual, expected)
        self.assertIsNone(renderer.executor)

    def test_worker_failure_falls_back_to_single_process(self) -> None:
        renderer = earth.ParallelRenderer(2)
        executor = mock.Mock()
        executor.map.side_effect = RuntimeError("worker failed")
        renderer.executor = executor
        state = earth.GlobeState()
        actual = renderer.render(state, 40, 12, False)
        expected = earth.render_globe(
            40,
            12,
            state.yaw,
            state.pitch,
            False,
            state.show_cities,
        )
        self.assertEqual(actual, expected)
        self.assertEqual(renderer.worker_count, 1)
        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)


class InputTests(unittest.TestCase):
    def test_fragmented_mouse_report(self) -> None:
        parser = earth.InputParser()
        self.assertEqual(parser.feed(b"\x1b[<0;12"), [])
        self.assertEqual(
            parser.feed(b";7M\x1b[<32;14;9M\x1b[<0;14;9m"),
            [
                ("mouse", 0, 12, 7, True),
                ("mouse", 32, 14, 9, True),
                ("mouse", 0, 14, 9, False),
            ],
        )

    def test_arrow_and_quit_keys(self) -> None:
        parser = earth.InputParser()
        self.assertEqual(
            parser.feed(b"\x1b[A\x1b[Dq"),
            [("key", "up"), ("key", "left"), ("key", "q")],
        )

    def test_lone_escape_is_flushed(self) -> None:
        parser = earth.InputParser()
        self.assertEqual(parser.feed(b"\x1b"), [])
        self.assertEqual(parser.flush_escape(), [("key", "escape")])

    def test_drag_changes_orientation(self) -> None:
        state = earth.GlobeState(yaw=0.0, pitch=0.0)
        self.assertTrue(state.handle_event(("mouse", 0, 20, 10, True), 80, 24))
        self.assertTrue(state.dragging)
        self.assertTrue(state.handle_event(("mouse", 32, 24, 12, True), 80, 24))
        self.assertNotEqual(state.yaw, 0.0)
        self.assertGreater(state.pitch, 0.0)
        self.assertTrue(state.handle_event(("mouse", 0, 24, 12, False), 80, 24))
        self.assertFalse(state.dragging)

    def test_click_selects_city_without_rotating(self) -> None:
        identifier, _, _, longitude, latitude = next(
            city for city in earth.NORDVPN_CITY_DATA if city[0] == "de_berlin"
        )
        projected = next(
            city
            for city in earth.project_cities(80, 23, longitude, latitude)
            if city.identifier == identifier
        )
        mouse_x = projected.cell[0] + 1
        mouse_y = projected.cell[1] + 1
        state = earth.GlobeState(yaw=longitude, pitch=latitude, speed=0.0)
        self.assertTrue(
            state.handle_event(("mouse", 0, mouse_x, mouse_y, True), 80, 24)
        )
        self.assertTrue(
            state.handle_event(("mouse", 0, mouse_x, mouse_y, False), 80, 24)
        )
        self.assertEqual(state.selected_city, "de_berlin")
        self.assertEqual(state.yaw, longitude)
        self.assertEqual(state.pitch, latitude)

        self.assertTrue(state.handle_event(("mouse", 0, 1, 1, True), 80, 24))
        self.assertTrue(state.handle_event(("mouse", 0, 1, 1, False), 80, 24))
        self.assertIsNone(state.selected_city)

    def test_pointer_motion_sets_and_clears_city_hover(self) -> None:
        identifier, _, _, longitude, latitude = next(
            city for city in earth.NORDVPN_CITY_DATA if city[0] == "de_berlin"
        )
        projected = next(
            city
            for city in earth.project_cities(80, 23, longitude, latitude)
            if city.identifier == identifier
        )
        state = earth.GlobeState(yaw=longitude, pitch=latitude, speed=0.0)
        state.handle_event(
            (
                "mouse",
                35,
                projected.cell[0] + 1,
                projected.cell[1] + 1,
                True,
            ),
            80,
            24,
        )
        self.assertEqual(state.hovered_city, "de_berlin")

        state.yaw += math.pi
        state.refresh_hover(80, 24)
        self.assertNotEqual(state.hovered_city, "de_berlin")

        state.handle_event(("mouse", 35, 1, 1, True), 80, 24)
        self.assertIsNone(state.hovered_city)

    def test_city_label_hover_and_click_open_confirmation(self) -> None:
        identifier, _, _, longitude, latitude = next(
            city for city in earth.NORDVPN_CITY_DATA if city[0] == "de_berlin"
        )
        projected = earth.project_cities(80, 23, longitude, latitude)
        label_cells, _ = earth._city_label_cells(
            projected,
            identifier,
            80,
            23,
        )
        label_x, label_y = next(iter(label_cells))
        mouse_x = label_x + 1
        mouse_y = label_y + 1
        state = earth.GlobeState(
            yaw=longitude,
            pitch=latitude,
            speed=0.0,
            selected_city=identifier,
        )

        state.handle_event(("mouse", 35, mouse_x, mouse_y, True), 80, 24)
        self.assertTrue(state.hovered_label)
        state.handle_event(("mouse", 0, mouse_x, mouse_y, True), 80, 24)
        state.handle_event(("mouse", 0, mouse_x, mouse_y, False), 80, 24)
        self.assertEqual(state.confirm_city, identifier)
        self.assertEqual(state.selected_city, identifier)

        state.handle_event(("key", "n"), 80, 24)
        self.assertIsNone(state.confirm_city)
        self.assertIsNone(state.pending_connect)

        state.handle_event(("mouse", 0, mouse_x, mouse_y, True), 80, 24)
        state.handle_event(("mouse", 0, mouse_x, mouse_y, False), 80, 24)
        state.handle_event(("key", "y"), 80, 24)
        self.assertEqual(state.pending_connect, identifier)
        self.assertIn("Connecting to Berlin", state.dialog_message)

    def test_wheel_report_does_not_start_dragging(self) -> None:
        state = earth.GlobeState()
        self.assertTrue(state.handle_event(("mouse", 64, 20, 10, True), 80, 24))
        self.assertFalse(state.dragging)

    def test_reset_uses_european_view_and_n_toggles_cities(self) -> None:
        state = earth.GlobeState(
            yaw=0.0,
            pitch=0.0,
            selected_city="de_berlin",
        )
        self.assertTrue(state.handle_event(("key", "r"), 80, 24))
        self.assertEqual(state.yaw, earth.DEFAULT_YAW)
        self.assertEqual(state.pitch, earth.DEFAULT_PITCH)
        self.assertIsNone(state.selected_city)
        self.assertTrue(state.show_cities)
        state.selected_city = "de_berlin"
        self.assertTrue(state.handle_event(("key", "n"), 80, 24))
        self.assertFalse(state.show_cities)
        self.assertIsNone(state.selected_city)

    def test_paused_and_dialog_states_disable_continuous_animation(self) -> None:
        state = earth.GlobeState()
        self.assertTrue(state.animation_active())
        state.paused = True
        self.assertFalse(state.animation_active())
        state.paused = False
        state.confirm_city = "de_berlin"
        self.assertFalse(state.animation_active())
        state.confirm_city = None
        state.dialog_message = "Connected."
        self.assertFalse(state.animation_active())

    def test_resuming_does_not_apply_idle_time_as_rotation(self) -> None:
        state = earth.GlobeState(yaw=1.0, paused=True)
        state.paused = False
        self.assertTrue(earth._advance_animation(state, 0.25, False))
        self.assertEqual(state.yaw, 1.0)
        self.assertTrue(earth._advance_animation(state, 0.25, True))
        self.assertGreater(state.yaw, 1.0)

    def test_pointer_coordinates_do_not_dirty_the_visual_state(self) -> None:
        state = earth.GlobeState()
        signature = state.visual_signature()
        state.mouse_x = 20
        state.mouse_y = 10
        state.pointer_known = True
        self.assertEqual(state.visual_signature(), signature)


class StarfieldTests(unittest.TestCase):
    def test_starfield_is_sparse_and_deterministic(self) -> None:
        first = [earth._star_mask(x, y) for y in range(24) for x in range(80)]
        second = [earth._star_mask(x, y) for y in range(24) for x in range(80)]
        self.assertEqual(first, second)
        star_count = sum(mask != 0 for mask in first)
        self.assertGreater(star_count, 20)
        self.assertLess(star_count, 100)

    def test_background_stays_fixed_while_globe_rotates(self) -> None:
        first = earth.render_globe(
            80,
            23,
            0.0,
            0.0,
            color=False,
            show_cities=False,
        )
        second = earth.render_globe(
            80,
            23,
            math.pi,
            0.0,
            color=False,
            show_cities=False,
        )
        self.assertEqual(first[0], second[0])
        self.assertTrue(any(character != " " for character in first[0]))


class TerminalModeTests(unittest.TestCase):
    def test_any_event_mouse_tracking_is_enabled_and_restored(self) -> None:
        self.assertIn("\x1b[?1003h", earth.TerminalSession.ENTER)
        self.assertIn("\x1b[?1006h", earth.TerminalSession.ENTER)
        self.assertIn("\x1b[?1003l", earth.TerminalSession.EXIT)

    def test_shutdown_flushes_queued_mouse_reports(self) -> None:
        session = object.__new__(earth.TerminalSession)
        session.input_fd = 10
        session.previous_settings = ["saved"]
        stdout = mock.Mock()
        stdout.fileno.return_value = 11
        with (
            mock.patch.object(earth.sys, "stdout", stdout),
            mock.patch.object(earth.termios, "tcdrain") as drain,
            mock.patch.object(earth.termios, "tcflush") as flush,
            mock.patch.object(earth.termios, "tcsetattr") as set_attributes,
        ):
            session.__exit__()

        drain.assert_called_once_with(11)
        flush.assert_called_once_with(10, earth.termios.TCIFLUSH)
        set_attributes.assert_called_once_with(
            10,
            earth.termios.TCSAFLUSH,
            ["saved"],
        )

    def test_default_frame_rate_is_24_fps(self) -> None:
        self.assertEqual(earth.parse_args([]).fps, 24.0)

    def test_worker_count_defaults_and_override(self) -> None:
        self.assertGreater(earth.DEFAULT_WORKERS, 1)
        self.assertLessEqual(earth.DEFAULT_WORKERS, 4)
        self.assertEqual(earth.parse_args(["--workers", "2"]).workers, 2)


if __name__ == "__main__":
    unittest.main()
