# LinkedIn Client Scraper

This repository contains the **LinkedIn Client Scraper**, a command-line executable that automates data extraction from LinkedIn using browser automation.  
The tool fetches structured data and exports it for further processing or integration.

---

### Features

- Automated scraping with browser control  
- Configurable rate limits and randomization  
- Export to CSV or JSON  
- Build pipeline triggered by GitHub tags  

---

### Prerequisites

- **Python 3.9+** or appropriate runtime  
- **Playwright** and browser dependencies installed  
- **GitHub Actions** configured for build automation (optional)

Install & build locally:

```bash
cd client
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python3 -m PyInstaller -y lnlabs-agent.spec
```

Artifacts appear in `dist/` (bundle + supporting folder). Run the CLI via `python -m lnlabs_agent.cli` or launch the Tk GUI with `python -m lnlabs_agent.gui` while developing.

### Runtime environments

By default the agent talks to `https://api.lnlabs.xyz`. To target a different backend without rebuilding:

- `API_BASE` — overrides the base URL entirely.
- `API_BASE_DEV` — optional Cloud Run/dev URL. When set, the CLI accepts `--env dev` and the GUI exposes a “dev” dropdown option.

Example (macOS/Linux):

```bash
export API_BASE_DEV=https://<your-cloud-run-url>
python3 -m lnlabs_agent.gui
```

For packaged app:

```bash
API_BASE_DEV=https://<your-cloud-run-url> ./dist/lnlabs-agent.app/Contents/MacOS/lnlabs-agent
```


Pick the environment before pairing; the UI/CLI shows the active base in the header/log.

---

### Versioning and Releases

Each release is triggered by pushing a **version tag** to GitHub.  
Follow this workflow when preparing a new version:

git add .
git commit -m "chore: client initial"
git push origin main


Then, create and push a new tag:

Replace version below with the next semantic version
git tag client-v1.0.1
git push origin client-v1.0.1


GitHub Actions will detect this tag and automatically trigger the build and release workflow.  
The build artifacts (e.g., executables or archives) will be available under **GitHub → Releases**.

---

### Tag Naming Convention

Use semantic versioning:

- `client-vX.Y.Z`  
  - `X`: Major changes or breaking updates  
  - `Y`: New features  
  - `Z`: Bug fixes or internal improvements  

Example:

- `client-v1.0.0` → Initial release  
- `client-v1.0.1` → Patch update  
- `client-v1.1.0` → Feature addition  

---

### License

This project is proprietary. No distribution of the executable or LinkedIn data is permitted without authorization.
