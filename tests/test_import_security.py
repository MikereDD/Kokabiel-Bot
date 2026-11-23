from __future__ import annotations

import stat
import tempfile
import unittest
import zipfile

from pathlib import Path

from imports.security import (
    ImportLimits,
    ImportSecurityError,
    inspect_import_file,
    neutralize_spreadsheet_formula,
)


class ImportSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def rejected(
        self,
        path: Path,
        code: str,
        *,
        limits: ImportLimits | None = None,
    ) -> None:
        kwargs = {}

        if limits is not None:
            kwargs["limits"] = limits

        with self.assertRaises(ImportSecurityError) as ctx:
            inspect_import_file(path, **kwargs)

        self.assertEqual(ctx.exception.code, code)

    def test_accepts_csv(self) -> None:
        result = inspect_import_file(
            self.write(
                "ratings.csv",
                b"Const,Your Rating,Title\n"
                b"tt0078748,10,Alien\n",
            )
        )

        self.assertEqual(result.kind, "csv")
        self.assertEqual(len(result.sha256), 64)

    def test_accepts_json(self) -> None:
        result = inspect_import_file(
            self.write(
                "history.json",
                b'{"items":[{"id":1}]}',
            )
        )

        self.assertEqual(result.kind, "json")

    def test_safe_zip_is_never_extracted(self) -> None:
        path = self.root / "tracker.zip"

        with zipfile.ZipFile(
            path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "movies.csv",
                "title,year\nAlien,1979\n",
            )
            archive.writestr(
                "episodes.json",
                '{"episodes":[1,2,3]}',
            )

        before = sorted(
            item.name
            for item in self.root.iterdir()
        )

        result = inspect_import_file(path)

        after = sorted(
            item.name
            for item in self.root.iterdir()
        )

        self.assertEqual(before, after)
        self.assertEqual(result.kind, "zip")
        self.assertEqual(
            {member.name for member in result.members},
            {"movies.csv", "episodes.json"},
        )

    def test_rejects_unknown_extension(self) -> None:
        self.rejected(
            self.write("payload.exe", b"MZ...."),
            "unsupported_type",
        )

    def test_rejects_executable_disguised_as_csv(self) -> None:
        self.rejected(
            self.write(
                "ratings.csv",
                b"MZ\x90\x00payload",
            ),
            "executable_content",
        )

    def test_rejects_binary_csv(self) -> None:
        self.rejected(
            self.write(
                "ratings.csv",
                b"title\x00year\n",
            ),
            "control_character",
        )

    def test_rejects_path_traversal(self) -> None:
        path = self.root / "bad.zip"

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "../escape.csv",
                "title\nAlien\n",
            )

        self.rejected(
            path,
            "path_traversal",
        )

    def test_rejects_absolute_path(self) -> None:
        path = self.root / "bad.zip"

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "/escape.csv",
                "title\nAlien\n",
            )

        self.rejected(
            path,
            "absolute_path",
        )

    def test_rejects_nested_archive(self) -> None:
        path = self.root / "bad.zip"

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "nested.zip",
                b"PK\x03\x04",
            )

        self.rejected(
            path,
            "archive_member_type",
        )

    def test_rejects_symlink_member(self) -> None:
        path = self.root / "bad.zip"

        info = zipfile.ZipInfo("link.csv")
        info.create_system = 3
        info.external_attr = (
            stat.S_IFLNK | 0o777
        ) << 16

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(info, "target")

        self.rejected(
            path,
            "special_file",
        )

    def test_rejects_member_limit(self) -> None:
        path = self.root / "many.zip"

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "a.csv",
                "a\n1\n",
            )
            archive.writestr(
                "b.csv",
                "b\n2\n",
            )

        self.rejected(
            path,
            "too_many_members",
            limits=ImportLimits(
                max_archive_members=1
            ),
        )

    def test_rejects_oversized_member(self) -> None:
        path = self.root / "big.zip"

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "big.csv",
                "x" * 100,
            )

        self.rejected(
            path,
            "member_too_large",
            limits=ImportLimits(
                max_member_bytes=20
            ),
        )

    def test_rejects_compression_bomb_ratio(self) -> None:
        path = self.root / "bomb.zip"

        with zipfile.ZipFile(
            path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "bomb.csv",
                "A" * 10_000,
            )

        self.rejected(
            path,
            "compression_bomb",
            limits=ImportLimits(
                max_compression_ratio=2.0
            ),
        )

    def test_rejects_deep_json(self) -> None:
        self.rejected(
            self.write(
                "deep.json",
                b"[[[1]]]",
            ),
            "json_too_deep",
            limits=ImportLimits(
                max_json_depth=2
            ),
        )

    def test_rejects_malformed_json(self) -> None:
        self.rejected(
            self.write(
                "bad.json",
                b'{"x":[1,2}}',
            ),
            "malformed_json",
        )

    def test_formula_neutralization(self) -> None:
        for value in (
            "=1+1",
            "+cmd",
            "-10+20",
            "@SUM(A1:A2)",
        ):
            self.assertTrue(
                neutralize_spreadsheet_formula(
                    value
                ).startswith("'")
            )

        self.assertEqual(
            neutralize_spreadsheet_formula(
                "Alien"
            ),
            "Alien",
        )


if __name__ == "__main__":
    unittest.main()
