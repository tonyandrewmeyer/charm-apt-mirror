# Copyright 2020 Ubuntu
# See LICENSE file for licensing details.

import datetime
import os
import random
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, Mock, call, mock_open, patch
from urllib.parse import urlparse
from uuid import uuid4

from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Context, Relation, State

from charm import AptMirrorCharm


def get_default_charm_configs():
    return {
        "mirror-list": "deb http://{0}/a {0}\ndeb http://{0}/b {0}".format(uuid4()),
        "base-path": str(uuid4()),
        "architecture": str(uuid4()),
        "threads": random.randint(10, 20),
        "use-proxy": True,
        "strip-mirror-name": False,
        # Harness allowed ``None`` here; Scenario's State wants a value that
        # matches the declared ``string`` type, so use the (falsy) empty
        # string. ``_build_subtree`` only tests this option for truthiness, so
        # the behaviour is identical to the old ``None``.
        "strip-mirror-path": "",
        "cron-schedule": str(uuid4()),
    }


@contextmanager
def begin(config=None):
    """Instantiate the charm with ``_stored.config`` populated.

    This replaces the old ``Harness``-based ``setUp``: it drives a
    ``config-changed`` event through a ``Context`` so the charm's stored
    state is synced from the (default) config, then yields the live charm
    object for the test to introspect and exercise directly.
    """
    ctx = Context(AptMirrorCharm)
    state = State(config=config if config is not None else get_default_charm_configs())
    # ``charm.open`` is patched so rendering ``/etc/apt/mirror.list`` and the
    # cron file during config-changed does not touch the real filesystem.
    with patch("charm.open", new_callable=mock_open):
        with ctx(ctx.on.config_changed(), state) as manager:
            manager.run()
            yield manager.charm


@contextmanager
def begin_action(ctx, action, params=None, config=None):
    """Set up an action event with ``_stored.config`` populated.

    Yields the Scenario ``Manager`` so the test can replace internal charm
    methods (the live charm is available as ``manager.charm``) before calling
    ``manager.run()`` to emit the action.
    """
    state = State(config=config if config is not None else get_default_charm_configs())
    event = ctx.on.action(action, params=params) if params else ctx.on.action(action)
    with patch("charm.open", new_callable=mock_open):
        with ctx(event, state) as manager:
            # Populate _stored.config (base-path etc.) just like the old setUp.
            manager.charm._on_config_changed(Mock())
            yield manager


def mock_repo_directory_tree(path, host, h_path, repo, c):
    return (["{}/{}/{}/{}".format(path, host, h_path, repo), ["{}".format(n)], n] for n in c)


class TestCharm(unittest.TestCase):
    def test_bad_mirror_list(self):
        bad_case_1 = """\
deb
"""
        bad_case_2 = """\
deb fake-uri
"""
        with begin() as charm:
            for test_case in [bad_case_1, bad_case_2]:
                with self.assertRaisesRegex(ValueError, "^An error .* option.$"):
                    charm._validate_mirror_list(test_case)

    def test_good_mirror_list(self):
        good_mirror_list = """\
deb fake-uri fake-distro fake-comp1

deb fake-uri fake-distro fake-comp1 fake-comp2
deb fake-uri fake-distro\
"""
        expected = [
            "deb fake-uri fake-distro fake-comp1",
            "deb fake-uri fake-distro fake-comp1 fake-comp2",
            "deb fake-uri fake-distro",
        ]
        with begin() as charm:
            returned = charm._validate_mirror_list(good_mirror_list)
        self.assertEqual(sorted(returned), sorted(expected))

    def test_update_status_not_synced(self):
        with begin() as charm:
            with patch("os.path.islink", return_value=False):
                charm._on_update_status(Mock())
            self.assertEqual(charm.model.unit.status, BlockedStatus("Packages not synchronized"))

    def test_update_status_not_published(self):
        class MockStat:
            st_mtime = 1

        with begin() as charm:
            with patch("os.path.islink", return_value=False), patch(
                "os.path.isdir", return_value=True
            ), patch("os.stat", return_value=MockStat()):
                charm._on_update_status(Mock())
            self.assertEqual(
                charm.model.unit.status,
                BlockedStatus("Last sync: {} not published".format(time.ctime(1))),
            )

    def test_update_status_published(self):
        snapshot_name = str(uuid4())
        with begin() as charm:
            with patch("os.path.islink", return_value=True), patch(
                "os.readlink", return_value="/tmp/{}".format(snapshot_name)
            ):
                charm._on_update_status(Mock())
            self.assertEqual(
                charm.model.unit.status,
                ActiveStatus("Publishes: {}".format(snapshot_name)),
            )

    def test_publish_relation_joined(self):
        config = get_default_charm_configs()
        relation = Relation("publish", remote_app_name="webserver")
        ctx = Context(AptMirrorCharm)
        with patch("charm.open", new_callable=mock_open):
            with ctx(
                ctx.on.relation_joined(relation, remote_unit=0),
                State(config=config, relations={relation}),
            ) as manager:
                # Populate _stored.config so base_path is available.
                manager.charm._on_config_changed(Mock())
                state_out = manager.run()
        relation_out = state_out.get_relation(relation.id)
        # Scenario also populates default network data (ingress-address etc.) in
        # the unit databag, so assert on the key the charm is responsible for
        # rather than the whole databag.
        self.assertEqual(
            relation_out.local_unit_data["path"],
            "{}/publish".format(config["base-path"]),
        )

    def test_install(self):
        ctx = Context(AptMirrorCharm)
        with patch("subprocess.check_output") as mock_subprocess_check_output:
            ctx.run(ctx.on.install(), State())
        mock_subprocess_check_output.assert_called_with(["apt", "install", "-y", "apt-mirror"])

    def test_cron_schedule_set(self):
        schedule = str(uuid4())
        config = get_default_charm_configs()
        config["cron-schedule"] = schedule
        ctx = Context(AptMirrorCharm)
        mock_open_call = mock_open()
        with patch("charm.open", mock_open_call):
            ctx.run(ctx.on.config_changed(), State(config=config))
        mock_open_call.assert_called_with("/etc/cron.d/{}".format("apt-mirror"), "w")
        mock_open_call.return_value.write.assert_called_with(
            "{} root apt-mirror\n".format(schedule)
        )

    def test_cron_schedule_remove(self):
        config = get_default_charm_configs()
        config["cron-schedule"] = ""
        with begin(config) as charm:
            # Mark the cron schedule as previously-set so re-running
            # config-changed detects the change to "" and removes the cron file.
            # (os.* is patched only around the direct handler call so it does not
            # interfere with Scenario's own tempdir teardown.)
            charm._stored.config["cron-schedule"] = str(uuid4())
            with patch("os.path.exists", return_value=True), patch("os.unlink") as os_unlink:
                charm._on_config_changed(Mock())
            os_unlink.assert_called_with("/etc/cron.d/{}".format("apt-mirror"))

    def test_apt_mirror_list(self):
        url = "http://archive.ubuntu.com/ubuntu"
        opts = "jammy main restricted universe multiverse"
        config = get_default_charm_configs()
        config["mirror-list"] = "deb {} {}".format(url, opts)
        # No cron schedule and no existing cron file, so the only file write is
        # the mirror.list render (mirrors the old test's single-write isolation,
        # which Harness got for free via incremental config updates).
        config["cron-schedule"] = ""
        ctx = Context(AptMirrorCharm)
        mocked_open = mock_open()
        with patch("charm.open", mocked_open), patch("os.path.exists", return_value=False):
            ctx.run(ctx.on.config_changed(), State(config=config))
        mocked_open.assert_called_with(Path("/etc/apt/mirror.list"), "wb")
        mocked_open().write.assert_called_once_with(
            "set base_path         {base-path}\n"
            "set mirror_path       $base_path/mirror\n"
            "set skel_path         $base_path/skel\n"
            "set var_path          $base_path/var\n"
            "set postmirror_script $var_path/postmirror.sh\n"
            "set defaultarch       {architecture}\n"
            "set run_postmirror    0\n"
            "set nthreads          {threads}\n"
            "set limit_rate        100m\n"
            "set _tilde            0\n"
            "{mirror-list}\n".format(**config).encode()
        )

    @patch.dict(
        os.environ,
        {"JUJU_CHARM_HTTP_PROXY": "httpproxy", "JUJU_CHARM_HTTPS_PROXY": "httpsproxy"},
        clear=True,
    )
    def test_juju_proxy(self):
        config = get_default_charm_configs()
        config["use-proxy"] = True
        config["cron-schedule"] = ""
        ctx = Context(AptMirrorCharm)
        mock_open_call = mock_open()
        with patch("charm.open", mock_open_call), patch("os.path.exists", return_value=False):
            ctx.run(ctx.on.config_changed(), State(config=config))
        mock_open_call.assert_called_with(Path("/etc/apt/mirror.list"), "wb")
        mock_open_call.return_value.write.assert_called_once_with(
            "set base_path         {base-path}\n"
            "set mirror_path       $base_path/mirror\n"
            "set skel_path         $base_path/skel\n"
            "set var_path          $base_path/var\n"
            "set postmirror_script $var_path/postmirror.sh\n"
            "set defaultarch       {architecture}\n"
            "set run_postmirror    0\n"
            "set nthreads          {threads}\n"
            "set limit_rate        100m\n"
            "set _tilde            0\n"
            "set use_proxy         on\n"
            "set http_proxy        httpproxy\n"
            "set https_proxy       httpsproxy\n"
            "{mirror-list}\n".format(**config).encode()
        )

    @patch.dict(
        os.environ,
        {"JUJU_CHARM_HTTP_PROXY": "httpproxy", "JUJU_CHARM_HTTPS_PROXY": "httpsproxy"},
        clear=True,
    )
    def test_juju_proxy_override(self):
        config = get_default_charm_configs()
        config["use-proxy"] = False
        config["cron-schedule"] = ""
        ctx = Context(AptMirrorCharm)
        mock_open_call = mock_open()
        with patch("charm.open", mock_open_call), patch("os.path.exists", return_value=False):
            ctx.run(ctx.on.config_changed(), State(config=config))
        mock_open_call.assert_called_with(Path("/etc/apt/mirror.list"), "wb")
        mock_open_call.return_value.write.assert_called_once_with(
            "set base_path         {base-path}\n"
            "set mirror_path       $base_path/mirror\n"
            "set skel_path         $base_path/skel\n"
            "set var_path          $base_path/var\n"
            "set postmirror_script $var_path/postmirror.sh\n"
            "set defaultarch       {architecture}\n"
            "set run_postmirror    0\n"
            "set nthreads          {threads}\n"
            "set limit_rate        100m\n"
            "set _tilde            0\n"
            "{mirror-list}\n".format(**config).encode()
        )

    def test_get_mirrors_without_filter(self):
        """Test git_mirrors without any filter applied."""
        archive = "http://archive.ubuntu.com/ubuntu"
        mirrors = [
            f"deb {archive} jammy-security main restricted universe multiverse",
            f"deb {archive} jammy-updates main restricted universe multiverse",
            f"deb {archive} jammy-proposed main restricted universe multiverse",
            f"deb {archive} jammy-backports main restricted universe multiverse",
        ]
        mirror_regex = ""
        exp_mirrors = mirrors
        with begin() as charm:
            charm._stored.config["mirror-list"] = mirrors
            filtered_mirrors = charm._get_mirrors(mirror_regex)
        self.assertListEqual(filtered_mirrors, exp_mirrors)

    def test_get_mirrors_with_filter(self):
        """Test git_mirrors without any filter applied."""
        archive = "http://archive.ubuntu.com/ubuntu"
        mirrors = [
            f"deb {archive} jammy-security main restricted universe multiverse",
            f"deb {archive} jammy-updates main restricted universe multiverse",
            f"deb {archive} jammy-proposed main restricted universe multiverse",
            f"deb {archive} jammy-backports main restricted universe multiverse",
            f"deb {archive} focal-updates main restricted universe multiverse",
        ]
        with begin() as charm:
            charm._stored.config["mirror-list"] = mirrors

            # based regex contains part of distribution
            mirror_regex = "updates"
            exp_mirrors = [
                f"deb {archive} jammy-updates main restricted universe multiverse",
                f"deb {archive} focal-updates main restricted universe multiverse",
            ]
            filtered_mirrors = charm._get_mirrors(mirror_regex)
            self.assertListEqual(filtered_mirrors, exp_mirrors)

            # based regex contains full source
            mirror_regex = f"deb {archive} jammy-updates main restricted universe multiverse"
            exp_mirrors = [f"deb {archive} jammy-updates main restricted universe multiverse"]
            filtered_mirrors = charm._get_mirrors(mirror_regex)
            self.assertListEqual(filtered_mirrors, exp_mirrors)

    def test_create_tmp_apt_mirror_config(self):
        """Test helper function to create tmp config for apt-mirror."""
        exp_path = "/tmp/test"
        exp_mirrors = [1, 2, 3]
        with begin() as charm:
            with mock.patch("charm.NamedTemporaryFile") as mock_tmp:
                mock_tmp.return_value.__enter__.return_value = mock_file = MagicMock()
                mock_file.name = exp_path
                charm._render_config = mock_render = MagicMock()
                path = charm._create_tmp_apt_mirror_config(*exp_mirrors)

                mock_tmp.assert_called_once_with(delete=False)
                mock_render.assert_called_once()
                args, _ = mock_render.call_args
                config, _ = args
                assert config.get("mirror-list") == tuple(exp_mirrors)
                assert path == Path(exp_path)

    def test_synchronize_action_without_source(self):
        ctx = Context(AptMirrorCharm)
        exp_mirrors = ["deb test1", "deb test2"]
        exp_config = "/tmp/test"
        exp_packages_to_clean = ["test"]
        with begin_action(ctx, "synchronize") as manager:
            charm = manager.charm
            charm._get_mirrors = mock_get_mirror = MagicMock(return_value=exp_mirrors)
            charm._create_tmp_apt_mirror_config = mock_create_tmp_apt_mirror_config = MagicMock(
                return_value=Path(exp_config)
            )
            charm._check_packages = Mock(return_value=(exp_packages_to_clean, "0.0 bytes"))
            base_path = Path(charm._stored.config["base-path"])
            with patch("charm.clean_dists") as mock_clean_dists, patch(
                "charm.clean_packages"
            ) as mock_clean_packages, patch("subprocess.check_output") as mock_check_output:
                manager.run()
            mock_get_mirror.assert_called_once_with(None)
            mock_clean_dists.assert_called_once_with(base_path)
            mock_create_tmp_apt_mirror_config.assert_called_once_with(*exp_mirrors)
            mock_check_output.assert_called_once_with(
                ["apt-mirror", exp_config], stderr=subprocess.STDOUT
            )
            mock_clean_packages.assert_called_once_with(exp_packages_to_clean)

    def test_synchronize_action_with_source(self):
        ctx = Context(AptMirrorCharm)
        exp_source = "deb test"
        exp_mirrors = ["deb test1", "deb test2"]
        exp_config = "/tmp/test"
        exp_packages_to_clean = ["test"]
        with begin_action(ctx, "synchronize", params={"source": exp_source}) as manager:
            charm = manager.charm
            charm._get_mirrors = mock_get_mirror = MagicMock(return_value=exp_mirrors)
            charm._create_tmp_apt_mirror_config = mock_create_tmp_apt_mirror_config = MagicMock(
                return_value=Path(exp_config)
            )
            charm._check_packages = Mock(return_value=(exp_packages_to_clean, "0.0 bytes"))
            with patch("charm.clean_dists") as mock_clean_dists, patch(
                "charm.clean_packages"
            ) as mock_clean_packages, patch("subprocess.check_output") as mock_check_output:
                manager.run()
            mock_get_mirror.assert_called_once_with(exp_source)
            mock_clean_dists.assert_not_called()
            mock_create_tmp_apt_mirror_config.assert_called_once_with(*exp_mirrors)
            mock_check_output.assert_called_once_with(
                ["apt-mirror", exp_config], stderr=subprocess.STDOUT
            )
            mock_clean_packages.assert_called_once_with(exp_packages_to_clean)

    def test_create_snapshot_action(self):
        config = get_default_charm_configs()
        config["strip-mirror-name"] = False
        config["mirror-list"] = "deb http://{0}/a {0}".format(uuid4())
        with begin(config) as charm:
            rand_subdir = str(random.randint(10, 100))
            upstream_path = "{}".format(uuid4())
            mirror_url = config["mirror-list"].split()[1]
            mirror_host = urlparse(mirror_url).hostname
            mirror_path = "{}/mirror".format(config["base-path"])
            snapshot_name = str(uuid4())
            with patch("charm.open", new_callable=mock_open), patch("os.walk") as os_walk, patch(
                "shutil.copytree"
            ) as shutil_copytree, patch("os.path.exists", return_value=False), patch(
                "os.symlink"
            ) as os_symlink, patch(
                "os.makedirs"
            ) as os_makedirs:
                os_walk.side_effect = iter(
                    [
                        mock_repo_directory_tree(
                            mirror_path, mirror_host, upstream_path, rand_subdir, ["pool", "dists"]
                        )
                    ]
                )
                charm._get_snapshot_name = Mock(return_value=snapshot_name)
                charm._on_create_snapshot_action(Mock())
            exp_src_root = charm.base_path / "mirror" / mirror_host / upstream_path / rand_subdir
            exp_dst_root = (
                charm.base_path / snapshot_name / mirror_host / upstream_path / rand_subdir
            )
            os_makedirs.assert_has_calls(
                [
                    call(charm.base_path / snapshot_name),
                    call(exp_dst_root, exist_ok=True),
                    call(exp_dst_root, exist_ok=True),
                ]
            )
            os_symlink.assert_called_once_with(exp_src_root / "pool", exp_dst_root / "pool")
            shutil_copytree.assert_called_once_with(exp_src_root / "dists", exp_dst_root / "dists")

    def test_create_snapshot_action_strip_mirrors(self):
        config = get_default_charm_configs()
        config["strip-mirror-name"] = True
        config["mirror-list"] = "deb http://{0}/a {0}".format(uuid4())
        with begin(config) as charm:
            rand_subdir = str(random.randint(10, 100))
            upstream_path = "{}".format(uuid4())
            mirror_url = config["mirror-list"].split()[1]
            mirror_host = urlparse(mirror_url).hostname
            mirror_path = "{}/mirror".format(config["base-path"])
            snapshot_name = str(uuid4())
            with patch("charm.open", new_callable=mock_open), patch("os.walk") as os_walk, patch(
                "shutil.copytree"
            ) as shutil_copytree, patch("os.path.exists", return_value=False), patch(
                "os.symlink"
            ) as os_symlink, patch(
                "os.makedirs"
            ) as os_makedirs:
                os_walk.side_effect = iter(
                    [
                        mock_repo_directory_tree(
                            mirror_path, mirror_host, upstream_path, rand_subdir, ["pool", "dists"]
                        )
                    ]
                )
                charm._get_snapshot_name = Mock(return_value=snapshot_name)
                charm._on_create_snapshot_action(Mock())
            exp_src_root = charm.base_path / "mirror" / mirror_host / upstream_path / rand_subdir
            exp_dst_root = charm.base_path / snapshot_name / upstream_path / rand_subdir
            os_makedirs.assert_has_calls(
                [
                    call(charm.base_path / snapshot_name),
                    call(exp_dst_root, exist_ok=True),
                    call(exp_dst_root, exist_ok=True),
                ]
            )
            os_symlink.assert_called_once_with(exp_src_root / "pool", exp_dst_root / "pool")
            shutil_copytree.assert_called_once_with(exp_src_root / "dists", exp_dst_root / "dists")

    def test_create_snapshot_action_strip_path(self):
        upstream_path = "{}".format(uuid4())
        config = get_default_charm_configs()
        config["strip-mirror-name"] = False
        config["mirror-list"] = "deb http://{0}/a {0}".format(uuid4())
        config["strip-mirror-path"] = "/{}".format(upstream_path)
        with begin(config) as charm:
            rand_subdir = str(random.randint(10, 100))
            mirror_url = config["mirror-list"].split()[1]
            mirror_host = urlparse(mirror_url).hostname
            mirror_path = "{}/mirror".format(config["base-path"])
            snapshot_name = str(uuid4())
            with patch("charm.open", new_callable=mock_open), patch("os.walk") as os_walk, patch(
                "shutil.copytree"
            ) as shutil_copytree, patch("os.path.exists", return_value=False), patch(
                "os.symlink"
            ) as os_symlink, patch(
                "os.makedirs"
            ) as os_makedirs:
                os_walk.side_effect = iter(
                    [
                        mock_repo_directory_tree(
                            mirror_path, mirror_host, upstream_path, rand_subdir, ["pool", "dists"]
                        )
                    ]
                )
                charm._get_snapshot_name = Mock(return_value=snapshot_name)
                charm._on_create_snapshot_action(Mock())
            exp_src_root = charm.base_path / "mirror" / mirror_host / upstream_path / rand_subdir
            exp_dst_root = charm.base_path / snapshot_name / mirror_host / rand_subdir
            os_makedirs.assert_has_calls(
                [
                    call(charm.base_path / snapshot_name),
                    call(exp_dst_root, exist_ok=True),
                    call(exp_dst_root, exist_ok=True),
                ]
            )
            os_symlink.assert_called_once_with(exp_src_root / "pool", exp_dst_root / "pool")
            shutil_copytree.assert_called_once_with(exp_src_root / "dists", exp_dst_root / "dists")

    def test_list_snapshots_action(self):
        snapshot_name = "snapshot-{}".format(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        snapshot = Mock()
        snapshot.name = snapshot_name
        for test_input, expected in [([snapshot], [snapshot_name]), ([], [])]:
            with self.subTest():
                ctx = Context(AptMirrorCharm)
                with begin_action(ctx, "list-snapshots") as manager:
                    manager.charm._list_snapshots = Mock(return_value=test_input)
                    manager.run()
                self.assertEqual(ctx.action_results, {"snapshots": expected})

    def test_delete_snapshot_action_success(self):
        snapshot_name = "snapshot-19700101"
        with begin() as charm:
            charm._get_snapshot_name = Mock()
            with patch("shutil.rmtree") as shutil_rmtree:
                charm._on_delete_snapshot_action(Mock(params={"name": snapshot_name}))
            shutil_rmtree.assert_called_once_with(charm.base_path / snapshot_name)

    def test_delete_snapshot_action_failure(self):
        with begin() as charm:
            for snapshot_name in ["", str(uuid4())]:
                with self.subTest(name=snapshot_name):
                    charm._get_snapshot_name = Mock()
                    with patch("shutil.rmtree") as shutil_rmtree:
                        charm._on_delete_snapshot_action(Mock(params={"name": snapshot_name}))
                        shutil_rmtree.assert_not_called()

    def test_publish_snapshot_action_success(self):
        snapshot_name = str(uuid4())
        with begin() as charm:
            charm._get_snapshot_name = Mock()
            base_path = charm.base_path
            with patch("os.path.isdir"), patch("os.path.islink", return_value=True), patch(
                "os.path.basename"
            ), patch("os.symlink") as os_symlink, patch("os.readlink"), patch("os.unlink"):
                charm._on_publish_snapshot_action(Mock(params={"name": snapshot_name}))
            os_symlink.assert_called_once_with(base_path / snapshot_name, base_path / "publish")

    def test_publish_snapshot_action_fail(self):
        snapshot_name = str(uuid4())
        with begin() as charm:
            charm._get_snapshot_name = Mock()
            action_event = Mock(params={"name": snapshot_name})
            with patch("os.path.isdir", return_value=False), patch("os.path.islink"), patch(
                "os.symlink"
            ), patch("os.unlink"):
                charm._on_publish_snapshot_action(action_event)
            self.assertEqual(action_event.fail.call_args, call("Snapshot does not exist"))

    def test_list_snapshots_not_empty(self):
        with tempfile.TemporaryDirectory() as tmpdirname:
            base_path = Path(tmpdirname)
            config = get_default_charm_configs()
            config["base-path"] = str(base_path)
            with begin(config) as charm:
                expected_snapshots = [base_path / "snapshot-1970010{}".format(i) for i in range(3)]
                for snapshot in expected_snapshots:
                    snapshot.mkdir(parents=True)
                returned_snapshots = charm._list_snapshots()
                self.assertEqual(sorted(expected_snapshots), sorted(returned_snapshots))

    def test_list_snapshots_empty(self):
        with tempfile.TemporaryDirectory() as tmpdirname:
            base_path = Path(tmpdirname)
            config = get_default_charm_configs()
            config["base-path"] = str(base_path)
            with begin(config) as charm:
                expected_snapshots = []
                returned_snapshots = charm._list_snapshots()
                self.assertEqual(expected_snapshots, sorted(returned_snapshots))

    def test_get_snapshot_name(self):
        with begin() as charm:
            snapshot_name = charm._get_snapshot_name()
        part_0 = snapshot_name.split("-")[0]
        part_1 = snapshot_name.split("-")[1]
        self.assertEqual(part_0, "snapshot")
        self.assertTrue(datetime.datetime.strptime(part_1, "%Y%m%d%H%M%S"))

    def test_check_packages_action(self):
        packages_to_be_removed = [Mock() for i in range(3)]
        ctx = Context(AptMirrorCharm)
        with begin_action(ctx, "check-packages") as manager:
            manager.charm._check_packages = Mock(
                return_value=[packages_to_be_removed, "0.0 bytes"]
            )
            manager.run()
        self.assertEqual(ctx.action_results["count"], 3)

    def test_clean_up_packages_action_false(self):
        ctx = Context(AptMirrorCharm)
        with begin_action(ctx, "clean-up-packages", params={"confirm": False}) as manager:
            manager.run()
        self.assertEqual(
            ctx.action_results,
            {"message": "Aborted! Please confirm your action with 'confirm=true'."},
        )

    def test_clean_up_packages_action_true(self):
        packages_to_be_removed = [Mock() for i in range(3)]
        ctx = Context(AptMirrorCharm)
        with begin_action(ctx, "clean-up-packages", params={"confirm": True}) as manager:
            manager.charm._check_packages = Mock(
                return_value=[packages_to_be_removed, "0.0 bytes"]
            )
            manager.run()
        for package in packages_to_be_removed:
            package.unlink.assert_called_once()
