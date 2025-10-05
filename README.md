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
- **GitHub Actions** configured for build automation  

Install dependencies:

pip install -r requirements.txt


The executable will appear in the `dist/` directory as `linkedin-client`.

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

