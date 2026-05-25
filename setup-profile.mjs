#!/usr/bin/env node

/**
 * Career-Ops Profile Setup
 *
 * Auto-generates profile.yml and cv.md from your resume and search.yml config.
 * Uses AI to extract resume information, no Python dependencies needed.
 *
 * Usage:
 *   node setup-profile.mjs                                      # Interactive setup
 *   node setup-profile.mjs --resume path/to/resume.pdf          # Use specific resume
 *   node setup-profile.mjs --auto                               # Minimal prompts
 *   node setup-profile.mjs --cli opencode                       # Use local LLM
 *   node setup-profile.mjs --resume resume.txt --cli opencode   # Combine options
 */

import fs from 'fs';
import path from 'path';
import readline from 'readline';
import { execSync } from 'child_process';
import YAML from 'yaml';
import pdfParse from 'pdf-parse';

const ROOT = process.cwd();
const CAREER_OPS_PATH = path.join(ROOT, 'career-ops');
const CONFIG_PATH = path.join(ROOT, 'config', 'search.yml');
const RESUME_ENV_PATH = path.join(ROOT, '.env');

// ============================================================
// Utilities
// ============================================================

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
});

function prompt(question) {
  return new Promise((resolve) => {
    rl.question(question, resolve);
  });
}

function log(msg) {
  console.log(`\n${msg}`);
}

function success(msg) {
  console.log(`✅ ${msg}`);
}

function warn(msg) {
  console.log(`⚠️  ${msg}`);
}

function error(msg) {
  console.error(`❌ ${msg}`);
}

// ============================================================
// CLI Configuration
// ============================================================

const CLI_COMMANDS = {
  claude: (prompt) => `claude -p "${escapePrompt(prompt)}"`,
  opencode: (prompt) => `opencode run "${escapePrompt(prompt)}"`,
  gemini: (prompt) => `gemini -p "${escapePrompt(prompt)}"`,
  copilot: (prompt) => `copilot -p "${escapePrompt(prompt)}"`,
  qwen: (prompt) => `qwen -p "${escapePrompt(prompt)}"`,
  codex: (prompt) => `codex exec "${escapePrompt(prompt)}"`,
};

function escapePrompt(prompt) {
  return prompt
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .slice(0, 5000); // Limit prompt size
}

// ============================================================
// Parse Arguments
// ============================================================

function parseArgs() {
  const args = process.argv.slice(2);
  const config = {
    resumePath: null,
    auto: false,
    force: false,
    cli: 'claude',
  };

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--resume' && args[i + 1]) {
      config.resumePath = args[++i];
    } else if (args[i] === '--auto') {
      config.auto = true;
    } else if (args[i] === '--force') {
      config.force = true;
    } else if (args[i] === '--cli' && args[i + 1]) {
      config.cli = args[++i];
    }
  }

  return config;
}

// ============================================================
// Check Prerequisites
// ============================================================

function checkPrerequisites() {
  if (!fs.existsSync(CAREER_OPS_PATH)) {
    error(`career-ops directory not found at ${CAREER_OPS_PATH}`);
    process.exit(1);
  }

  if (!fs.existsSync(CONFIG_PATH)) {
    error(`search.yml not found at ${CONFIG_PATH}`);
    process.exit(1);
  }

  return true;
}

// ============================================================
// Load Search Config
// ============================================================

function loadSearchConfig() {
  const content = fs.readFileSync(CONFIG_PATH, 'utf-8');
  return YAML.parse(content);
}

// ============================================================
// Find or Locate Resume
// ============================================================

async function findResume(cliPath) {
  // Check CLI path
  if (cliPath) {
    if (fs.existsSync(cliPath)) return cliPath;
    error(`Resume not found at: ${cliPath}`);
    process.exit(1);
  }

  // Check .env RESUME_PATH
  if (fs.existsSync(RESUME_ENV_PATH)) {
    const envContent = fs.readFileSync(RESUME_ENV_PATH, 'utf-8');
    const match = envContent.match(/RESUME_PATH=(.+)/);
    if (match) {
      const envPath = match[1].trim().replace(/^["']|["']$/g, '');
      const resolvedPath = path.isAbsolute(envPath)
        ? envPath
        : path.join(ROOT, envPath);
      if (fs.existsSync(resolvedPath)) {
        return resolvedPath;
      }
    }
  }

  // Check default locations
  const defaultLocations = [
    path.join(ROOT, 'resumes', 'resume.pdf'),
    path.join(ROOT, 'resume.pdf'),
    path.join(ROOT, 'cv.pdf'),
  ];

  for (const loc of defaultLocations) {
    if (fs.existsSync(loc)) return loc;
  }

  // Ask user
  log('Resume not found automatically.');
  const resumePath = await prompt(
    'Enter path to your resume (PDF or text file): '
  );
  if (!fs.existsSync(resumePath)) {
    error(`File not found: ${resumePath}`);
    process.exit(1);
  }

  return resumePath;
}

// ============================================================
// Extract Resume Text
// ============================================================

async function extractResumeText(resumePath) {
  if (resumePath.endsWith('.pdf')) {
    try {
      const pdfBuffer = fs.readFileSync(resumePath);
      const data = await pdfParse(pdfBuffer);
      return data.text || null;
    } catch (e) {
      warn(`PDF extraction failed: ${e.message}`);
      warn('Falling back to AI extraction...');
      return null; // Signal to use AI extraction
    }
  } else {
    return fs.readFileSync(resumePath, 'utf-8');
  }
}

// ============================================================
// AI-Powered Resume Extraction
// ============================================================

async function extractResumeViaAI(resumePath, cli = 'claude') {
  log('📋 Using AI to extract resume information...');

  let resumeContent;

  if (resumePath.endsWith('.pdf')) {
    // For PDFs, tell the user to provide text version or we'll use a different approach
    warn('For PDF files, please convert to text or copy-paste the content.');
    return null;
  } else {
    resumeContent = fs.readFileSync(resumePath, 'utf-8');
  }

  const prompt = `Extract the following information from this resume. Return ONLY valid JSON, no markdown or extra text.

RESUME:
${resumeContent}

Extract and return JSON with this structure (use null for missing fields):
{
  "full_name": "string",
  "email": "string or null",
  "phone": "string or null",
  "location": "City, State or null",
  "linkedin": "linkedin.com/in/username or null",
  "github": "github.com/username or null",
  "summary": "1-2 sentence professional summary or null",
  "skills": ["skill1", "skill2", ...],
  "experience": [
    {
      "company": "Company Name",
      "title": "Job Title",
      "duration": "Dates",
      "description": "Brief summary"
    }
  ],
  "projects": [
    {
      "name": "Project Name",
      "description": "What it does",
      "url": "GitHub/portfolio URL or null"
    }
  ]
}

Return ONLY the JSON object, nothing else.`;

  const cliCommand = CLI_COMMANDS[cli];
  if (!cliCommand) {
    error(`Unknown CLI: ${cli}`);
    process.exit(1);
  }

  try {
    const command = cliCommand(prompt);
    const output = execSync(command, {
      encoding: 'utf-8',
      maxBuffer: 10 * 1024 * 1024,
    });

    // Parse JSON from response
    const jsonMatch = output.match(/\{[\s\S]*\}/);
    if (!jsonMatch) {
      error('Could not parse AI response as JSON');
      return null;
    }

    const extracted = JSON.parse(jsonMatch[0]);
    success('Resume information extracted via AI');
    return extracted;
  } catch (e) {
    error(`AI extraction failed: ${e.message}`);
    return null;
  }
}

// ============================================================
// Parse Resume Info
// ============================================================

function parseResumeInfo(resumeText) {
  const info = {
    name: null,
    email: null,
    phone: null,
    location: null,
    linkedin: null,
    github: null,
  };

  // Try to extract email
  const emailMatch = resumeText.match(/([a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+\.[a-zA-Z0-9_-]+)/);
  if (emailMatch) info.email = emailMatch[1];

  // Try to extract phone (US format)
  const phoneMatch = resumeText.match(/\+?1?\s*\(?(\d{3})\)?\s*[-.\s]?(\d{3})[-.\s]?(\d{4})/);
  if (phoneMatch) info.phone = `+1 (${phoneMatch[1]}) ${phoneMatch[2]}-${phoneMatch[3]}`;

  // Try to extract LinkedIn
  const linkedinMatch = resumeText.match(/linkedin\.com\/in\/([a-zA-Z0-9-]+)/i);
  if (linkedinMatch) info.linkedin = `linkedin.com/in/${linkedinMatch[1]}`;

  // Try to extract GitHub
  const githubMatch = resumeText.match(/github\.com\/([a-zA-Z0-9-]+)/i);
  if (githubMatch) info.github = `github.com/${githubMatch[1]}`;

  // Try to extract location (look for patterns like "City, State")
  const locationMatch = resumeText.match(/\|?\s*([A-Z][a-z]+,\s*[A-Z]{2})/);
  if (locationMatch) info.location = locationMatch[1];

  // Try to extract first line as name (often the first line)
  const firstLine = resumeText.trim().split('\n')[0].trim();
  if (firstLine && firstLine.length < 100 && !firstLine.includes('@')) {
    info.name = firstLine;
  }

  return info;
}

// ============================================================
// Prompt for Missing Info
// ============================================================

async function promptForInfo(parsed, autoMode) {
  const info = { ...parsed };

  if (!info.name) {
    if (autoMode) {
      warn('Name not found in resume. Using "Your Name"');
      info.name = 'Your Name';
    } else {
      info.name = await prompt('Full name: ');
    }
  }

  if (!info.email) {
    if (autoMode) {
      warn('Email not found in resume.');
      info.email = 'your.email@example.com';
    } else {
      info.email = await prompt('Email address: ');
    }
  }

  if (!info.phone) {
    if (!autoMode) {
      const p = await prompt('Phone (optional, press enter to skip): ');
      if (p) info.phone = p;
    }
  }

  if (!info.location) {
    if (autoMode) {
      info.location = 'Your City, State';
    } else {
      info.location = await prompt(
        'Location (city, state) [default: Your City, State]: '
      ) || 'Your City, State';
    }
  }

  if (!info.linkedin) {
    if (!autoMode) {
      const li = await prompt(
        'LinkedIn URL (optional, press enter to skip): '
      );
      if (li) info.linkedin = li;
    }
  }

  if (!info.github) {
    if (!autoMode) {
      const gh = await prompt('GitHub URL (optional, press enter to skip): ');
      if (gh) info.github = gh;
    }
  }

  // Infer timezone from location
  if (!info.timezone) {
    const locationToTz = {
      'dallas': 'CST',
      'texas': 'CST',
      'chicago': 'CST',
      'denver': 'MST',
      'phoenix': 'MST',
      'san francisco': 'PST',
      'california': 'PST',
      'seattle': 'PST',
      'new york': 'EST',
      'boston': 'EST',
      'florida': 'EST',
    };

    const cityLower = (info.location || '').toLowerCase();
    info.timezone = 'UTC';
    for (const [city, tz] of Object.entries(locationToTz)) {
      if (cityLower.includes(city)) {
        info.timezone = tz;
        break;
      }
    }
  }

  return info;
}

// ============================================================
// Prompt for Role Criteria
// ============================================================

async function promptForRoleCriteria(autoMode) {
  const criteria = {
    targetRoles: [],
    negativeRoles: [],
    compensationTarget: '$130K-170K',
    compensationMin: '$110K',
    locationFlexibility: 'Remote preferred',
  };

  if (autoMode) {
    warn('Skipping role criteria (auto mode). Edit config/search.yml and career-ops/config/profile.yml later.');
    return criteria;
  }

  console.log('\n📋 Job Search Criteria');
  console.log('='.repeat(60));

  // Target roles
  const targetInput = await prompt(
    '\nTarget roles (comma-separated)?\nExample: "Senior Full-Stack Engineer, Mobile Engineer"\n→ '
  );
  if (targetInput) {
    criteria.targetRoles = targetInput
      .split(',')
      .map(r => r.trim())
      .filter(r => r);
  } else {
    criteria.targetRoles = ['Software Engineer', 'Full-Stack Engineer'];
  }

  // Negative roles (roles to avoid)
  const negativeInput = await prompt(
    '\nRoles to avoid (comma-separated, optional)?\nExample: "Junior, Intern, Manager"\n→ '
  );
  if (negativeInput) {
    criteria.negativeRoles = negativeInput
      .split(',')
      .map(r => r.trim())
      .filter(r => r);
  } else {
    criteria.negativeRoles = ['Junior', 'Intern', 'Manager'];
  }

  // Compensation
  const compTarget = await prompt(
    '\nTarget compensation range? [default: $130K-170K]\n→ '
  );
  if (compTarget) criteria.compensationTarget = compTarget;

  const compMin = await prompt(
    '\nMinimum acceptable salary? [default: $110K]\n→ '
  );
  if (compMin) criteria.compensationMin = compMin;

  // Location
  const locInput = await prompt(
    '\nLocation preference? [default: Remote preferred]\n→ '
  );
  if (locInput) criteria.locationFlexibility = locInput;

  return criteria;
}

// ============================================================
// Search Settings (locations, distance, hours_old, etc.)
// ============================================================

// US state codes (50 + DC). Used to auto-infer country_indeed and to make the
// comma-aware location parser re-join "City, ST" pairs.
const US_STATES = new Set([
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
  'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
  'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
  'VA','WA','WV','WI','WY','DC',
]);

// Canadian province / territory codes.
const CA_PROVINCES = new Set([
  'AB','BC','MB','NB','NL','NS','ON','PE','QC','SK','NT','NU','YT',
]);

/**
 * Parse a comma-separated location string while keeping "City, ST" pairs together.
 *
 * Input:  "US Remote, Dallas, TX, Fort Worth, TX"
 * Output: ["US Remote", "Dallas, TX", "Fort Worth, TX"]
 *
 * Heuristic: after splitting on commas, any 2-letter uppercase token is treated
 * as a continuation of the previous chunk (state/province code).
 */
function parseLocations(input) {
  if (!input) return [];
  const parts = input.split(',').map(s => s.trim()).filter(Boolean);
  const out = [];
  let i = 0;
  while (i < parts.length) {
    const cur = parts[i];
    const next = parts[i + 1];
    if (next && /^[A-Z]{2}$/.test(next)) {
      out.push(`${cur}, ${next}`);
      i += 2;
    } else {
      out.push(cur);
      i += 1;
    }
  }
  return out;
}

/**
 * Auto-infer JobSpy's `country_indeed` field from a free-text location.
 * Falls back to "USA" so the workflow has a sane default — if it's wrong, the
 * user gets a clear error from JobSpy on first run.
 */
function inferCountry(location) {
  const m = location.match(/,\s*([A-Z]{2})\s*$/);
  if (m) {
    if (US_STATES.has(m[1])) return 'USA';
    if (CA_PROVINCES.has(m[1])) return 'Canada';
  }
  if (/\b(us|usa|united states|america)\b/i.test(location)) return 'USA';
  if (/\bcanad(a|ian)\b/i.test(location)) return 'Canada';
  if (/\b(uk|united kingdom|britain|england|scotland|wales)\b/i.test(location)) return 'UK';
  if (/\baustralia\b/i.test(location)) return 'Australia';
  return 'USA';
}

/**
 * Convert a remote location string into the JobSpy `location:` field. Remote
 * searches use a country-level location plus `is_remote: true`.
 */
function inferRemoteLocation(country) {
  switch (country) {
    case 'Canada':    return 'Canada';
    case 'UK':        return 'United Kingdom';
    case 'Australia': return 'Australia';
    default:          return 'United States';
  }
}

/**
 * Build the per-location structures used by updateSearchConfig.
 * Each entry: { raw, isRemote, location, country, distance? }.
 * Non-remote entries get a `distance` field (prompted from the user).
 */
async function buildLocationEntries(parsedLocations) {
  const entries = [];
  for (const raw of parsedLocations) {
    const isRemote = /\bremote\b/i.test(raw);
    const country = inferCountry(raw);
    const entry = {
      raw,
      isRemote,
      country,
      location: isRemote ? inferRemoteLocation(country) : raw,
    };
    if (!isRemote) {
      const distInput = await prompt(`   Distance from "${raw}" in miles? [50] → `);
      const dist = parseInt(distInput, 10);
      entry.distance = Number.isFinite(dist) && dist > 0 ? dist : 50;
    }
    entries.push(entry);
  }
  return entries;
}

async function promptForSearchSettings(autoMode) {
  // Defaults that work without any prompting — used in --auto mode and as the
  // fallback for empty answers in interactive mode.
  const settings = {
    locations: [{ raw: 'United States', isRemote: true, location: 'United States', country: 'USA' }],
    hoursOld: 24,
    resultsWanted: 100,
    sites: ['indeed', 'linkedin', 'glassdoor'],
    includeEasyApply: false,
  };

  if (autoMode) {
    warn('Skipping search settings (auto mode). Edit config/search.yml later.');
    return settings;
  }

  console.log('\n🔍 Search Settings');
  console.log('='.repeat(60));

  // ── Locations ──────────────────────────────────────────────────────────
  console.log('\n📍 Locations to search (comma-separated).');
  console.log('   "City, ST" pairs are kept together. "Remote" anywhere in a');
  console.log('   chunk routes that pass through JobSpy\'s is_remote filter.');
  console.log('   Examples:');
  console.log('     Dallas, TX');
  console.log('     US Remote, Dallas, TX, Fort Worth, TX');
  console.log('     Toronto, ON, Montreal, QC');
  console.log('     London, UK');
  const locInput = await prompt('\n→ ');
  const parsed = parseLocations(locInput);
  if (parsed.length === 0) {
    warn('No locations entered; defaulting to "US Remote".');
  } else {
    console.log(`\n   Parsed ${parsed.length} location(s): ${parsed.map(l => `"${l}"`).join(', ')}`);
    settings.locations = await buildLocationEntries(parsed);
  }

  // ── hours_old ──────────────────────────────────────────────────────────
  const hoursInput = await prompt(
    '\nHow recent should results be? (24 = today, 168 = past week) [24]\n→ '
  );
  const hours = parseInt(hoursInput, 10);
  if (Number.isFinite(hours) && hours > 0) settings.hoursOld = hours;

  // ── results_wanted ─────────────────────────────────────────────────────
  const resultsInput = await prompt(
    '\nMax results per site per search term? [100]\n→ '
  );
  const results = parseInt(resultsInput, 10);
  if (Number.isFinite(results) && results > 0) settings.resultsWanted = results;

  // ── sites ──────────────────────────────────────────────────────────────
  console.log('\nWhich boards? Comma-separated. Options: linkedin, indeed, glassdoor, zip_recruiter, google');
  const sitesInput = await prompt('[linkedin, indeed, glassdoor]\n→ ');
  if (sitesInput) {
    const sites = sitesInput.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    if (sites.length > 0) settings.sites = sites;
  }

  // ── easy-apply pass ────────────────────────────────────────────────────
  const easyInput = await prompt(
    '\nInclude an "easy apply" pass? Runs every 4h via the cloud workflow. [y/N]\n→ '
  );
  settings.includeEasyApply = /^y/i.test(easyInput);

  return settings;
}


// ============================================================
// Prompt for Career Narrative (for _profile.md)
// ============================================================

async function promptForCareerNarrative(autoMode, _info, criteria) {
  const narrative = {
    exitStory: '',
    dealBreakers: [],
    locationPolicy: {},
    portfolio: [],
  };

  if (autoMode) {
    warn('Skipping career narrative (auto mode). Edit career-ops/modes/_profile.md later.');
    return narrative;
  }

  console.log('\n📖 Career Narrative & Preferences');
  console.log('='.repeat(60));

  // Exit story
  const exitStoryInput = await prompt(
    '\nTell me your career transition story (what brings you to these roles?).\nExample: "Transitioning from platform engineering to full-stack roles where I own the whole product"\n→ '
  );
  if (exitStoryInput) {
    narrative.exitStory = exitStoryInput;
  } else {
    narrative.exitStory = `Transitioning to ${criteria.targetRoles[0]} roles where I can ship complete products.`;
  }

  // Deal-breakers
  console.log('\n🚫 Deal-breakers (non-negotiables):');
  const dealBreakerInput = await prompt(
    'What are you NOT interested in? (comma-separated)\nExample: "Legacy codebases only, Startup <10 people, Manager roles"\n→ '
  );
  if (dealBreakerInput) {
    narrative.dealBreakers = dealBreakerInput
      .split(',')
      .map(d => d.trim())
      .filter(d => d);
  } else {
    narrative.dealBreakers = ['Legacy codebases only (no greenfield)', 'Startup with <10 people'];
  }

  // Location preferences — preferred already captured in role criteria
  console.log('\n🌍 Location Policy:');
  narrative.locationPolicy.preferred = criteria.locationFlexibility || 'Remote preferred';

  const locationFlexInput = await prompt(
    'What flexibility do you have? (e.g., "Occasional travel OK", "1-2 weeks/month on-site possible")\n→ '
  );
  narrative.locationPolicy.flexibility = locationFlexInput || 'Flexible for right opportunity';

  // Portfolio/proof points
  console.log('\n🎯 Portfolio & Proof Points (optional):');
  const portfolioInput = await prompt(
    'Any key projects, articles, or portfolios? (comma-separated URLs or descriptions)\n→ '
  );
  if (portfolioInput) {
    narrative.portfolio = portfolioInput
      .split(',')
      .map(p => p.trim())
      .filter(p => p);
  }

  return narrative;
}

// ============================================================
// Generate _profile.md
// ============================================================

function generateProfileMarkdown(info, criteria, narrative) {
  let markdown = `# User Profile Context -- career-ops

<!-- ============================================================
     THIS FILE IS YOURS. It will NEVER be auto-updated.

     Customize everything here: your archetypes, narrative,
     proof points, negotiation scripts, location policy.

     The system reads _shared.md (updatable) first, then this
     file (your overrides). Your customizations always win.
     ============================================================ -->

## Your Target Roles

| Archetype | Thematic axes | What they buy |
|-----------|---------------|---------------|
`;

  // Generate rows for each target role
  criteria.targetRoles.forEach((role) => {
    const roleDescriptions = {
      'full-stack': '| **Full-Stack Engineer** | Shipping, end-to-end ownership, rapid iteration | Someone who builds complete products solo or in small teams |',
      'mobile': '| **Mobile/Cross-Platform Engineer** | Expo, React Native, Flutter, multiplatform | Someone who ships beautiful apps to iOS/Android/Web at scale |',
      'backend': '| **Backend Engineer** | APIs, databases, performance, reliability | Someone who builds robust systems that don\'t break |',
      'frontend': '| **Frontend Engineer** | UI/UX, performance, accessibility, design systems | Someone who builds beautiful, performant user experiences |',
      'devops': '| **Cloud/DevOps Engineer** | AWS, infrastructure, CI/CD, automation | Someone who builds the platform others ship on |',
      'sre': '| **Production Support / SRE** | On-call, incident response, operational excellence | Someone who keeps systems alive in production |',
      'lead': '| **Technical Lead** | System design, mentoring, architectural decisions | Someone who elevates the team |',
      'principal': '| **Principal/Staff Engineer** | Architecture, strategy, organizational impact | Someone who shapes the future of the platform |',
    };

    let found = false;
    for (const [key, desc] of Object.entries(roleDescriptions)) {
      if (role.toLowerCase().includes(key)) {
        markdown += desc + '\n';
        found = true;
        break;
      }
    }

    if (!found) {
      markdown += `| **${role}** | Ownership, technical excellence, impact | Someone who excels in this domain |\n`;
    }
  });

  markdown += `
## Your Adaptive Framing

| If the role is... | Emphasize about you... | Proof point sources |
|-------------------|------------------------|---------------------|
`;

  criteria.targetRoles.forEach((role) => {
    const emphasis = {
      'full-stack': 'End-to-end ownership, shipping products, full tech stack',
      'mobile': 'Modern mobile tooling, cross-platform expertise, shipping at scale',
      'backend': 'Scalable APIs, database design, system reliability',
      'frontend': 'User experience, performance optimization, design systems',
      'devops': 'Infrastructure automation, CI/CD pipelines, operational excellence',
      'sre': '24/7 on-call reliability, incident response, automation',
      'lead': 'System design, team mentoring, technical decision-making',
      'principal': 'Organizational impact, architectural vision, strategic influence',
    };

    let emphasisText = 'Your unique strengths and experience';
    for (const [key, val] of Object.entries(emphasis)) {
      if (role.toLowerCase().includes(key)) {
        emphasisText = val;
        break;
      }
    }

    markdown += `| ${role} | ${emphasisText} | cv.md |\n`;
  });

  markdown += `
## Your Exit Narrative

${narrative.exitStory}

Frame yourself as:
${criteria.targetRoles.slice(0, 3).map((role) => {
  const frames = {
    'full-stack': '"I ship complete products end-to-end. From architecture to frontend to production support."',
    'mobile': '"I\'m a modern mobile engineer. React Native, Expo, Flutter. I ship to iOS/Android/Web simultaneously."',
    'backend': '"I\'m a systems builder. APIs, databases, production reliability, CI/CD automation."',
    'frontend': '"I build performant, beautiful UIs. Design systems, accessibility, modern tooling."',
    'devops': '"I build the platform others ship on. Infrastructure automation, reliability, and scale."',
    'sre': '"I love keeping systems alive. 24/7 on-call, incident response, operations expertise."',
    'lead': '"I elevate teams through technical leadership and mentorship."',
  };

  let frame = 'Someone who excels in this domain';
  for (const [key, val] of Object.entries(frames)) {
    if (role.toLowerCase().includes(key)) {
      frame = val;
      break;
    }
  }

  return `- **For ${role} roles:** ${frame}`;
}).join('\n')}

## Your Location Policy

**Preferred:** ${narrative.locationPolicy.preferred || 'Remote'}

**Flexibility:** ${narrative.locationPolicy.flexibility || 'Flexible for right opportunity'}

**Timezone:** ${info.timezone || 'UTC-6'} — comfortable with async teams

**In evaluations (scoring):**
- **Remote roles:** Score 5.0 (ideal)
- **Preferred location (${info.location}):** Score 5.0 (already there)
- **Hybrid, US-based with <20% on-site:** Score 4.0 (flexible)
- **Hybrid, non-US with on-site required:** Score 2.0 (visa complexity)
- **On-site 4-5 days/week, no exceptions:** Score 1.0 (not viable)

## Deal-breakers

(Add any non-negotiables here as you learn them)

${narrative.dealBreakers.map(d => `- ${d}`).join('\n')}

## Your Comp Targets

Current market data for your roles:
${criteria.targetRoles.slice(0, 4).map(role => `- **${role}:** \`check levels.fyi for current data\``).join('\n')}

**Your target:** ${criteria.compensationTarget} base (adjust up for stock/bonus to total comp)

## Negotiation Scripts

**Salary expectations:**
> "Based on market data for this role, I'm targeting ${criteria.compensationTarget}. I'm flexible on structure—what matters is the total package and the opportunity to ship impactful products."

**When asked about current comp:**
> "I'm focused on finding the right opportunity and role fit. My target for the market and my skillset is ${criteria.compensationTarget}. What's the range for this position?"

**When offered below target:**
> "I appreciate the offer. I'm comparing with opportunities in the $${criteria.compensationTarget.split('-')[0]} range. I'm genuinely interested because [reason]. Can we explore a package closer to my target?"

## Portfolio & Proof Points

${narrative.portfolio.length > 0 ? narrative.portfolio.map(p => `- ${p}`).join('\n') : '(Add your key projects, articles, or portfolios here)'}
`;

  return markdown;
}

// ============================================================
// Generate CV Markdown
// ============================================================

function generateCV(resumeText, info) {
  // Try to extract sections from resume
  const sections = {
    summary: '',
    experience: '',
    projects: '',
    education: '',
    skills: '',
  };

  // Match both markdown headers and all-caps section headers
  const sectionRegex = /(?:^#+\s*|^)((?:PROFESSIONAL\s+)?SUMMARY|EXPERIENCE|PROJECTS?|(?:PROJECTS\s+&\s+OUTSIDE\s+)?EXPERIENCE|EDUCATION|SKILLS|CERTIFICATIONS?)\s*\n/im;
  const splits = resumeText.split(sectionRegex);

  for (let i = 0; i < splits.length - 1; i += 2) {
    const header = (splits[i + 1] || '').toLowerCase();
    const content = (splits[i + 2] || '').trim();

    if (
      header.includes('summary')
    ) sections.summary = content;
    else if (header.includes('experience')) sections.experience = content;
    else if (header.includes('project')) sections.projects = content;
    else if (header.includes('education')) sections.education = content;
    else if (header.includes('skill')) sections.skills = content;
  }

  // Build markdown CV
  let cv = `# ${info.name}\n\n`;

  // Contact info
  const contact = [];
  if (info.phone) contact.push(info.phone);
  if (info.email) contact.push(info.email);
  if (info.location) contact.push(info.location);
  if (info.linkedin) contact.push(`[LinkedIn](https://${info.linkedin})`);
  if (info.github) contact.push(`[GitHub](https://${info.github})`);

  if (contact.length > 0) {
    cv += contact.join(' | ') + '\n\n';
  }

  if (sections.summary) {
    cv += '## Professional Summary\n\n' + sections.summary + '\n\n';
  }

  if (sections.skills) {
    cv += '## Skills\n\n' + sections.skills + '\n\n';
  }

  if (sections.experience) {
    cv += '## Professional Experience\n\n' + sections.experience + '\n\n';
  }

  if (sections.projects) {
    cv += '## Projects\n\n' + sections.projects + '\n\n';
  }

  if (sections.education) {
    cv += '## Education\n\n' + sections.education + '\n\n';
  }

  return cv;
}

// ============================================================
// Generate Profile YAML
// ============================================================

function generateProfile(info, criteria) {
  // Detect seniority level from target and negative roles
  const targetRolesLower = criteria.targetRoles.map(r => r.toLowerCase()).join(' ');
  const negativeRolesLower = criteria.negativeRoles.map(r => r.toLowerCase()).join(' ');

  let seniority = 'mid';
  if (targetRolesLower.includes('senior') || targetRolesLower.includes('staff')) {
    seniority = 'senior/staff';
  } else if (targetRolesLower.includes('principal') || targetRolesLower.includes('lead')) {
    seniority = 'staff/principal';
  } else if (negativeRolesLower.includes('senior') && !targetRolesLower.includes('senior')) {
    seniority = 'mid/junior';
  }

  // Create archetypes from user's target roles
  const archetypes = criteria.targetRoles.map((role, idx) => ({
    name: role,
    level: seniority,
    fit: idx === 0 ? 'primary' : 'secondary',
  }));

  // Estimate timezone from location
  const locationToTz = {
    'dallas': 'CST',
    'texas': 'CST',
    'chicago': 'CST',
    'san francisco': 'PST',
    'california': 'PST',
    'new york': 'EST',
    'boston': 'EST',
  };

  const cityLower = (info.location || '').toLowerCase();
  let timezone = 'UTC-6';
  for (const [city, tz] of Object.entries(locationToTz)) {
    if (cityLower.includes(city)) {
      timezone = tz;
      break;
    }
  }

  const profile = {
    candidate: {
      full_name: info.name,
      email: info.email,
      phone: info.phone || '',
      location: info.location,
      linkedin: info.linkedin || '',
      github: info.github || '',
      portfolio_url: '',
    },
    target_roles: {
      primary: criteria.targetRoles.slice(0, 2),
      archetypes: archetypes,
    },
    narrative: {
      headline: 'Software engineer building impactful products',
      exit_story:
        'Passionate about shipping quality software and solving real problems.',
      superpowers: [
        'Full-stack development',
        'Problem-solving',
        'Learning quickly',
      ],
      proof_points: [],
    },
    compensation: {
      target_range: criteria.compensationTarget,
      currency: 'USD',
      minimum: criteria.compensationMin,
      location_flexibility: criteria.locationFlexibility,
    },
    location: {
      country: 'United States',
      city: (info.location || '').split(',')[0].trim(),
      timezone: timezone,
      visa_status: 'No sponsorship needed',
    },
    cv: {
      output_format: 'html',
    },
  };

  return profile;
}

// ============================================================
// Write Files
// ============================================================

function writeProfile(profile) {
  const profilePath = path.join(CAREER_OPS_PATH, 'config', 'profile.yml');
  const yaml_str = YAML.stringify(profile);
  fs.writeFileSync(profilePath, yaml_str, 'utf-8');
  return profilePath;
}

// ============================================================
// Update Search Config
// ============================================================

/**
 * Convert a user's target-role list into JobSpy-friendly search terms.
 * Adds lowercase variants for "full-stack" / "fullstack" / non-senior forms
 * so we cover the common job-board phrasings.
 */
function expandSearchTerms(targetRoles) {
  const out = [];
  for (const role of targetRoles) {
    const lower = role.toLowerCase();
    out.push(lower);
    if (lower.includes('full-stack') || lower.includes('full stack')) {
      out.push('full-stack');
      out.push('fullstack');
    }
    if (lower.includes('senior')) {
      const withoutSenior = lower.replace(/senior\s+/i, '').trim();
      if (withoutSenior) out.push(withoutSenior);
    }
  }
  return [...new Set(out)];
}

/**
 * Build a single JobSpy pass object from a location entry + global settings.
 * The shape matches config/search.example.yml. JobSpy mutex rules (Indeed):
 *   - Remote pass uses is_remote (NO hours_old — they're mutually exclusive).
 *   - Local pass uses hours_old + distance.
 *   - Easy-apply pass uses easy_apply (NO hours_old, NO is_remote).
 */
function buildPass({ name, searchTerms, sites, resultsWanted, location, country, isRemote, distance, hoursOld, easyApply }) {
  const pass = {
    name,
    // Clone the arrays so the resulting YAML doesn't share references across
    // passes — otherwise YAML.stringify emits `&a1` / `*a1` anchors that
    // confuse users reading config/search.yml.
    search_terms: [...searchTerms],
    sites: [...sites],
    results_wanted: resultsWanted,
    location,
    country_indeed: country,
    // Description backfill happens in the screen stage from the same HTTP
    // response used for liveness — see config/search.example.yml.
    linkedin_fetch_description: false,
  };
  if (easyApply) {
    pass.easy_apply = true;
  } else if (isRemote) {
    pass.is_remote = true;
  } else {
    pass.hours_old = hoursOld;
    pass.distance = distance;
  }
  return pass;
}

/**
 * Rewrite the `searches:` block of config/search.yml based on user input.
 *
 * Strategy:
 *   - One scrape pass per user location (recent local, or remote variant).
 *   - Optionally one "easy apply" pass using the first non-remote location
 *     (or the first remote one if all are remote).
 *   - `filter:` keeps its existing structure; we only refresh target_titles /
 *     negative_titles. `screen:` is preserved untouched if present.
 */
function updateSearchConfig(targetRoles, negativeRoles, searchSettings) {
  const searchConfigPath = path.join(process.cwd(), 'config', 'search.yml');

  if (!fs.existsSync(searchConfigPath)) {
    warn(`Search config not found at ${searchConfigPath}. Skipping sync.`);
    return null;
  }

  const content = fs.readFileSync(searchConfigPath, 'utf-8');
  const config = YAML.parse(content) || {};

  const searchTerms = expandSearchTerms(targetRoles);

  // ── searches: ──────────────────────────────────────────────────────────
  const { locations, hoursOld, resultsWanted, sites, includeEasyApply } = searchSettings;
  const passes = [];
  for (const loc of locations) {
    passes.push(buildPass({
      name: loc.raw,
      searchTerms,
      sites,
      resultsWanted,
      location: loc.location,
      country: loc.country,
      isRemote: loc.isRemote,
      distance: loc.distance,
      hoursOld,
    }));
  }
  if (includeEasyApply) {
    // Prefer a non-remote location for the easy-apply pass so JobSpy filters
    // by city. If all the user's locations are remote, fall back to the first.
    const anchor = locations.find(l => !l.isRemote) || locations[0];
    passes.push(buildPass({
      name: 'easy apply',
      searchTerms,
      sites,
      resultsWanted,
      location: anchor.location,
      country: anchor.country,
      easyApply: true,
    }));
  }
  config.searches = passes;
  // Remove the legacy single-pass shorthand if it was in the file.
  delete config.search;

  // ── filter: ────────────────────────────────────────────────────────────
  if (!config.filter) config.filter = {};
  config.filter.target_titles = searchTerms;
  if (negativeRoles && negativeRoles.length > 0) {
    config.filter.negative_titles = negativeRoles;
  }

  // ── screen: ────────────────────────────────────────────────────────────
  // Default to liveness on for new configs. Preserves any existing screen
  // settings the user may have customized on a re-run.
  if (!config.screen) {
    config.screen = { liveness: true, liveness_timeout: 8 };
  }

  // Write back
  const yaml_str = YAML.stringify(config);
  fs.writeFileSync(searchConfigPath, yaml_str, 'utf-8');
  return searchConfigPath;
}

function writeCV(cv) {
  const cvPath = path.join(CAREER_OPS_PATH, 'cv.md');
  fs.writeFileSync(cvPath, cv, 'utf-8');
  return cvPath;
}

function writeProfileMarkdown(markdown) {
  const profileMdPath = path.join(CAREER_OPS_PATH, 'modes', '_profile.md');

  // Create modes directory if it doesn't exist
  const modesDir = path.dirname(profileMdPath);
  if (!fs.existsSync(modesDir)) {
    fs.mkdirSync(modesDir, { recursive: true });
  }

  fs.writeFileSync(profileMdPath, markdown, 'utf-8');
  return profileMdPath;
}

// ============================================================
// Main
// ============================================================

async function main() {
  console.log('\n🚀 Career-Ops Profile Setup\n');

  const args = parseArgs();

  // Validate CLI
  if (!CLI_COMMANDS[args.cli]) {
    error(`Unknown CLI: ${args.cli}`);
    error(`Available: ${Object.keys(CLI_COMMANDS).join(', ')}`);
    rl.close();
    process.exit(1);
  }
  checkPrerequisites();

  // Check if files already exist
  const profilePath = path.join(CAREER_OPS_PATH, 'config', 'profile.yml');
  const cvPath = path.join(CAREER_OPS_PATH, 'cv.md');

  if (fs.existsSync(profilePath) && fs.existsSync(cvPath) && !args.force) {
    warn('Profile and CV already exist.');
    const overwrite = await prompt('Overwrite? (y/n): ');
    if (overwrite.toLowerCase() !== 'y') {
      console.log('Aborted.\n');
      rl.close();
      return;
    }
  }

  // Load search config
  const searchConfig = loadSearchConfig();
  log(`Loaded search config with ${searchConfig.filter?.target_titles?.length || 0} target roles`);

  // Find resume
  let resumePath = args.resumePath || process.env.RESUME_PATH;
  resumePath = await findResume(resumePath);
  log(`Found resume: ${resumePath}`);

  // Extract resume text
  log('Extracting resume text...');
  let resumeText = await extractResumeText(resumePath);
  let parsed = {};

  if (!resumeText) {
    // Use AI extraction as fallback
    log('Using AI to extract resume information...');
    const aiExtracted = await extractResumeViaAI(resumePath, args.cli);
    if (aiExtracted) {
      parsed = aiExtracted;
      resumeText = JSON.stringify(aiExtracted); // For CV generation
    } else {
      error('Could not extract text from resume.');
      rl.close();
      process.exit(1);
    }
  } else {
    // Parse resume info from text
    parsed = parseResumeInfo(resumeText);
    log('Parsed resume information');
  }

  // Prompt for missing info
  const info = await promptForInfo(parsed, args.auto);

  // Prompt for role criteria (search + evaluation sync)
  const criteria = await promptForRoleCriteria(args.auto);

  // Prompt for search settings (locations, distance, hours_old, etc.) — these
  // drive the `searches:` block of config/search.yml.
  const searchSettings = await promptForSearchSettings(args.auto);

  // Prompt for career narrative (for _profile.md)
  const narrative = await promptForCareerNarrative(args.auto, info, criteria);

  // Generate files
  log('Generating profile.yml, cv.md, and _profile.md...');
  const profile = generateProfile(info, criteria);
  const cv = generateCV(resumeText, info);
  const profileMarkdown = generateProfileMarkdown(info, criteria, narrative);

  // Write files
  const profileFile = writeProfile(profile);
  const cvFile = writeCV(cv);
  const profileMdFile = writeProfileMarkdown(profileMarkdown);
  const searchFile = updateSearchConfig(criteria.targetRoles, criteria.negativeRoles, searchSettings);

  success(`Profile saved: ${profileFile}`);
  success(`CV saved: ${cvFile}`);
  success(`Career narrative saved: ${profileMdFile}`);
  if (searchFile) {
    success(`Search config updated: ${searchFile}`);
  }

  // Summary
  log('\n📋 Setup Complete!\n');
  console.log(`Your profile, CV, and career guidance are now synchronized:`);
  console.log(`  - CV: ${cvFile}`);
  console.log(`  - Profile (config): ${profileFile}`);
  console.log(`  - Career Narrative: ${profileMdFile}`);
  if (searchFile) {
    console.log(`  - Search Filter: ${searchFile}`);
  }
  console.log(`\n🎯 Configuration:`);
  console.log(`  - Target Roles: ${criteria.targetRoles.join(', ')}`);
  console.log(`  - Compensation: ${criteria.compensationTarget} (min: ${criteria.compensationMin})`);
  console.log(`  - Location preference: ${criteria.locationFlexibility}`);
  console.log(`  - Deal-breakers: ${narrative.dealBreakers.join(', ') || 'None specified'}`);
  console.log(`\n🔍 Search Settings (written to config/search.yml):`);
  for (const loc of searchSettings.locations) {
    const detail = loc.isRemote
      ? `remote → location="${loc.location}"`
      : `local, distance=${loc.distance}mi`;
    console.log(`  - "${loc.raw}" — ${detail} (country_indeed=${loc.country})`);
  }
  console.log(`  - hours_old: ${searchSettings.hoursOld}`);
  console.log(`  - results_wanted: ${searchSettings.resultsWanted}`);
  console.log(`  - sites: ${searchSettings.sites.join(', ')}`);
  console.log(`  - easy-apply pass: ${searchSettings.includeEasyApply ? 'yes' : 'no'}`);
  console.log(`\n📚 Next Steps:\n`);
  console.log(`Step 1️⃣  — Review your setup:`);
  console.log(`  • Profile: ${profileFile}`);
  console.log(`  • CV: ${cvFile}`);
  console.log(`  • Career Narrative: ${profileMdFile}`);
  console.log(`  • Search Config: config/search.yml (synced with your target roles)\n`);
  console.log(`Step 2️⃣  — Scrape and filter jobs:`);
  console.log(`  Windows:  .\\run.ps1`);
  console.log(`  macOS/Linux: ./run.sh\n`);
  console.log(`  This populates career-ops/data/pipeline.md with filtered job postings.\n`);
  console.log(`Step 3️⃣  — Use career-ops to evaluate jobs:`);
  console.log(`  With your CLI of choice (Claude Code, Opencode, Gemini, etc.):\n`);
  console.log(`  cd career-ops`);
  console.log(`  /career-ops         # See all available commands`);
  console.log(`  /career-ops-scan    # Scan for new jobs`);
  console.log(`  /career-ops         # Paste a job URL to evaluate it\n`);
  console.log(`  Or run the full pipeline on all jobs in pipeline.md:\n`);
  console.log(`  Your CLI's batch/pipeline command (see career-ops/AGENTS.md)\n`);
  console.log(`💡 Your profile, CV, and career narrative are ready to go.`);
  console.log(`   Career-ops will use them automatically for evaluation.\n`);
  console.log();

  rl.close();
}

main().catch((err) => {
  error(err.message);
  rl.close();
  process.exit(1);
});
