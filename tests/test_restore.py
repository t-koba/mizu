import io
import tarfile
from support import Fixture
from mizu.errors import Denied
from mizu.project import Project
from mizu.storage import backup, restore


class RestoreTests(Fixture):
    def test_roundtrip_preserves_uncommitted_code_and_remains_unarmed(self):
        self.project.set_control(paused=True)
        (self.project.workspace / 'app.py').write_text('VALUE = 7\n')
        archive = self.root / 'saved.tar.gz'
        backup(self.project, archive)
        result = restore(self.config, 'restored', archive)
        restored = Project(self.config, 'restored')
        self.assertFalse(result['armed'])
        self.assertTrue(restored.control()['paused'])
        self.assertEqual((restored.workspace / 'app.py').read_text(), 'VALUE = 7\n')
        self.assertEqual(restored.snapshots.get()['outcome'], 'wait')
        self.assertTrue((restored.root / 'spool/editor/.mizu-outbox').is_file())
        with self.assertRaises(Denied):
            restore(self.config, 'restored', archive)

    def malicious(self, name, kind=None):
        archive = self.root / 'malicious.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo(name)
            if kind:
                info.type = kind
                info.linkname = 'escape'
            else:
                info.size = 3
            tar.addfile(info, None if kind else io.BytesIO(b'bad'))
        return archive

    def test_refuses_traversal(self):
        with self.assertRaises(Denied):
            restore(self.config, 'restored', self.malicious('../escape'))
        self.assertFalse((self.config.data / 'projects/restored').exists())

    def test_refuses_symlink_and_hardlink(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.assertRaises(Denied):
                restore(self.config, 'restored', self.malicious('objects/escape', kind))

    def test_refuses_large_archive(self):
        with self.assertRaises(Denied):
            restore(self.config, 'restored', self.malicious('backup.json'), max_bytes=2)

    def test_refuses_unexpected_top_level(self):
        with self.assertRaises(Denied):
            restore(self.config, 'restored', self.malicious('workspace/exploit'))
