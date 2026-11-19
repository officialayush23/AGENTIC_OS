"""Deterministic detection of well-known project configuration files."""

from __future__ import annotations

import fnmatch
from typing import Optional, Tuple

# (pattern, ecosystem, kind, purpose). Patterns are matched against the
# lowercase file name; patterns containing '/' are matched against the path.
CONFIG_PATTERNS = [
    ("package.json", "Node.js", "manifest", "npm package metadata, scripts and dependencies"),
    ("package-lock.json", "Node.js", "lockfile", "Exact npm dependency versions"),
    ("yarn.lock", "Node.js", "lockfile", "Exact Yarn dependency versions"),
    ("pnpm-lock.yaml", "Node.js", "lockfile", "Exact pnpm dependency versions"),
    ("pnpm-workspace.yaml", "Node.js", "workspace", "pnpm monorepo definition"),
    (".nvmrc", "Node.js", "runtime", "Node.js version pin"),
    ("tsconfig.json", "TypeScript", "compiler", "TypeScript compiler options"),
    ("tsconfig.*.json", "TypeScript", "compiler", "TypeScript compiler options (variant)"),
    ("jsconfig.json", "JavaScript", "compiler", "JavaScript project / editor options"),
    ("vite.config.*", "Vite", "build", "Vite dev server and bundler configuration"),
    ("vitest.config.*", "Vite", "test", "Vitest test runner configuration"),
    ("webpack.config.*", "Webpack", "build", "Webpack bundler configuration"),
    ("rollup.config.*", "Rollup", "build", "Rollup bundler configuration"),
    ("next.config.*", "Next.js", "framework", "Next.js framework configuration"),
    ("nuxt.config.*", "Nuxt", "framework", "Nuxt framework configuration"),
    ("svelte.config.*", "Svelte", "framework", "Svelte configuration"),
    ("astro.config.*", "Astro", "framework", "Astro configuration"),
    ("angular.json", "Angular", "framework", "Angular workspace configuration"),
    ("tailwind.config.*", "Tailwind CSS", "styling", "Tailwind CSS theme and content configuration"),
    ("postcss.config.*", "PostCSS", "styling", "PostCSS plugin configuration"),
    ("components.json", "shadcn/ui", "ui", "shadcn/ui component configuration"),
    (".eslintrc*", "ESLint", "lint", "JavaScript lint rules"),
    ("eslint.config.*", "ESLint", "lint", "JavaScript lint rules (flat config)"),
    (".prettierrc*", "Prettier", "format", "Code formatting rules"),
    ("prettier.config.*", "Prettier", "format", "Code formatting rules"),
    ("babel.config.*", "Babel", "build", "Babel transpiler configuration"),
    (".babelrc", "Babel", "build", "Babel transpiler configuration"),
    ("jest.config.*", "Jest", "test", "Jest test runner configuration"),
    ("pyproject.toml", "Python", "manifest", "Python project metadata, dependencies and tool settings"),
    ("requirements*.txt", "Python", "dependencies", "Python package requirements"),
    ("setup.py", "Python", "packaging", "Python packaging script"),
    ("setup.cfg", "Python", "packaging", "Python packaging / tool configuration"),
    ("pipfile", "Python", "dependencies", "Pipenv dependencies"),
    ("pipfile.lock", "Python", "lockfile", "Pipenv lockfile"),
    ("poetry.lock", "Python", "lockfile", "Poetry lockfile"),
    ("uv.lock", "Python", "lockfile", "uv lockfile"),
    ("tox.ini", "Python", "test", "tox test environments"),
    ("pytest.ini", "Python", "test", "pytest configuration"),
    ("conftest.py", "Python", "test", "pytest fixtures"),
    ("mypy.ini", "Python", "lint", "mypy type checker configuration"),
    ("ruff.toml", "Python", "lint", "Ruff linter configuration"),
    (".flake8", "Python", "lint", "flake8 linter configuration"),
    (".python-version", "Python", "runtime", "Python version pin"),
    ("environment.yml", "Conda", "environment", "Conda environment definition"),
    ("cargo.toml", "Rust", "manifest", "Rust crate manifest"),
    ("cargo.lock", "Rust", "lockfile", "Rust dependency lockfile"),
    ("tauri.conf.json", "Tauri", "desktop", "Tauri desktop app configuration"),
    ("go.mod", "Go", "manifest", "Go module definition"),
    ("go.sum", "Go", "lockfile", "Go dependency checksums"),
    ("pom.xml", "Java", "manifest", "Maven project definition"),
    ("build.gradle", "Java/Kotlin", "build", "Gradle build script"),
    ("build.gradle.kts", "Java/Kotlin", "build", "Gradle Kotlin build script"),
    ("settings.gradle*", "Java/Kotlin", "build", "Gradle settings"),
    ("*.csproj", ".NET", "manifest", "C# project file"),
    ("*.fsproj", ".NET", "manifest", "F# project file"),
    ("*.sln", ".NET", "solution", "Visual Studio solution"),
    ("global.json", ".NET", "runtime", ".NET SDK version pin"),
    ("nuget.config", ".NET", "dependencies", "NuGet package sources"),
    ("directory.build.props", ".NET", "build", "MSBuild shared properties"),
    ("composer.json", "PHP", "manifest", "Composer package manifest"),
    ("gemfile", "Ruby", "dependencies", "Ruby gem dependencies"),
    ("mix.exs", "Elixir", "manifest", "Elixir project definition"),
    ("pubspec.yaml", "Dart/Flutter", "manifest", "Dart / Flutter package manifest"),
    ("cmakelists.txt", "C/C++", "build", "CMake build definition"),
    ("makefile", "Make", "build", "Make build targets"),
    ("meson.build", "C/C++", "build", "Meson build definition"),
    ("dockerfile", "Docker", "container", "Container image build instructions"),
    ("*.dockerfile", "Docker", "container", "Container image build instructions"),
    ("docker-compose*.yml", "Docker", "container", "Multi-container service definition"),
    ("docker-compose*.yaml", "Docker", "container", "Multi-container service definition"),
    ("compose.yml", "Docker", "container", "Multi-container service definition"),
    ("compose.yaml", "Docker", "container", "Multi-container service definition"),
    (".dockerignore", "Docker", "container", "Files excluded from the Docker build context"),
    (".gitignore", "Git", "repository", "Files Git should ignore"),
    (".gitattributes", "Git", "repository", "Git path attributes"),
    (".editorconfig", "Editor", "format", "Cross-editor formatting rules"),
    (".github/workflows/*.yml", "GitHub Actions", "ci", "CI workflow"),
    (".github/workflows/*.yaml", "GitHub Actions", "ci", "CI workflow"),
    (".gitlab-ci.yml", "GitLab CI", "ci", "CI pipeline"),
    ("azure-pipelines.yml", "Azure Pipelines", "ci", "CI pipeline"),
    ("jenkinsfile", "Jenkins", "ci", "CI pipeline"),
    (".env.example", "Environment", "template", "Template of required environment variables (no secrets)"),
    (".env.sample", "Environment", "template", "Template of required environment variables (no secrets)"),
    ("vercel.json", "Vercel", "deploy", "Deployment configuration"),
    ("netlify.toml", "Netlify", "deploy", "Deployment configuration"),
    ("app.json", "Expo/React Native", "manifest", "App configuration"),
]

DOC_PATTERNS = ["readme*", "license*", "changelog*", "contributing*", "security*", "code_of_conduct*"]


def classify_config(rel_path: str) -> Optional[Tuple[str, str, str]]:
    """Return (ecosystem, kind, purpose) if the path is a known configuration file."""
    lower_path = rel_path.lower()
    name = lower_path.rsplit("/", 1)[-1]
    for pattern, ecosystem, kind, purpose in CONFIG_PATTERNS:
        if "/" in pattern:
            if fnmatch.fnmatchcase(lower_path, pattern) or fnmatch.fnmatchcase(lower_path, "*/" + pattern):
                return ecosystem, kind, purpose
        elif fnmatch.fnmatchcase(name, pattern):
            return ecosystem, kind, purpose
    return None


def is_doc_file(rel_path: str) -> bool:
    name = rel_path.lower().rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, p) for p in DOC_PATTERNS)
