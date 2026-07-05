# Contributing to AniFlow

Thanks for your interest in contributing to AniFlow! This guide will help you get started.

## 🚀 Quick Start for Contributors

1. **Fork and clone** the repository
2. **Set up your environment** following the main README
3. **Create a branch** for your feature: `git checkout -b feature/your-feature-name`
4. **Make your changes** and test them
5. **Commit** with clear, descriptive messages
6. **Push** and create a pull request

## 🎯 Areas Where We Need Help

- **New scraper adapters** — support for more manhwa/manga sites
- **UI improvements** — the Flask frontend could use polish
- **Performance** — optimizing the video rendering pipeline
- **Documentation** — tutorials, examples, troubleshooting guides
- **Testing** — unit tests, integration tests
- **Bug fixes** — check the Issues page

## 🔧 Development Setup

```bash
# Standard setup from README, plus:
pip install -r requirements-dev.txt  # If we add dev dependencies later

# Run the app in debug mode
export FLASK_ENV=development
python app.py
```

## 📝 Code Style

- **Python**: Follow PEP 8. Use meaningful variable names.
- **Comments**: Explain *why*, not *what* (the code shows what).
- **Imports**: Group by standard library, third-party, local. Sort alphabetically.

## 🧪 Testing Your Changes

Before submitting a PR:

1. **Test the full pipeline** on at least one manhwa chapter
2. **Check both modes** (Dialogue Recap and Narrated Recap if relevant)
3. **Verify no regressions** — existing features still work
4. **Clean output** — no console errors or warnings

## 📬 Submitting a Pull Request

1. **Keep PRs focused** — one feature or fix per PR
2. **Write a clear description**:
   - What does this change?
   - Why is it needed?
   - How did you test it?
3. **Reference related issues**: "Fixes #123" or "Relates to #456"
4. **Be responsive** to review feedback

## 🐛 Reporting Bugs

When reporting a bug, include:

- **Your environment**: OS, Python version, Ollama version
- **Steps to reproduce** the issue
- **Expected vs actual behavior**
- **Logs or screenshots** if helpful
- **The manhwa site** if it's a scraper issue

## 💡 Suggesting Features

Feature requests are welcome! Please:

- **Check existing issues** first to avoid duplicates
- **Describe the use case** — what problem does this solve?
- **Propose a solution** if you have one in mind
- **Consider scope** — does this fit AniFlow's mission?

## 🏗️ Project Structure

```
├── app.py                  # Flask app + main routes
├── config.py               # Configuration & paths
├── pipeline/               # Core processing modules
│   ├── web_scraper.py     # Download manhwa chapters
│   ├── panel_cropper.py   # Detect & crop panels
│   ├── comic_text_remover.py  # Clean speech bubbles
│   ├── narrator.py        # AI narration generation
│   ├── kokoro_tts.py      # TTS engines
│   ├── video_assembler.py # Render video clips
│   └── ...
├── templates/             # HTML templates
├── static/                # CSS, JS, images
├── models/                # YOLO weights (gitignored)
└── manhwa_*/              # Generated data (gitignored)
```

## 🤝 Code of Conduct

- Be respectful and constructive
- Welcome newcomers
- Focus on the work, not the person
- Assume good intentions

## 📄 License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

**Questions?** Open a discussion or issue — we're here to help!
