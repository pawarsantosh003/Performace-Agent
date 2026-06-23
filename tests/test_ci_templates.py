import unittest
from pathlib import Path


class CITemplateTests(unittest.TestCase):
    def test_github_workflow_comments_uploads_and_enforces_gate(self) -> None:
        workflow = Path(".github/workflows/release-readiness-gate.yml").read_text(encoding="utf-8")

        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("actions/github-script@v7", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn("GATE_EXIT_CODE", workflow)
        self.assertIn("Performance gate blocked the release", workflow)

    def test_other_ci_templates_preserve_warning_and_block_semantics(self) -> None:
        jenkins = Path("Jenkinsfile").read_text(encoding="utf-8")
        gitlab = Path(".gitlab-ci.yml").read_text(encoding="utf-8")
        azure = Path("azure-pipelines.yml").read_text(encoding="utf-8")

        self.assertIn("unstable(", jenkins)
        self.assertIn("archiveArtifacts", jenkins)
        self.assertIn("allow_failure:", gitlab)
        self.assertIn("when: always", gitlab)
        self.assertIn("scripts/post_gitlab_performance_comment.py", gitlab)
        self.assertIn("task.logissue type=warning", azure)
        self.assertIn("condition: always()", azure)

    def test_pipeline_documentation_and_sample_config_exist(self) -> None:
        self.assertTrue(Path("docs/ci_cd_release_gate.md").exists())
        self.assertTrue(Path("examples/sample_pipeline_report.md").exists())
        self.assertTrue(Path("examples/ci_release_gate_config.json").exists())
        self.assertTrue(Path("scripts/post_gitlab_performance_comment.py").exists())


if __name__ == "__main__":
    unittest.main()
