/* Shared navigation for every documentation page.
 *
 * The site is static with no build step, so five pages would otherwise mean
 * five copies of the navbar and sidebar to keep in sync. Defining the
 * structure once here and rendering it at load means a new page or a renamed
 * section is a one-line change in one file.
 *
 * Each page sets `document.body.dataset.page` to its own id; that drives the
 * active tab and which sidebar group is shown.
 */

(() => {
  "use strict";

  const PAGES = [
    {
      id: "index",
      file: "index.html",
      tab: "Overview",
      links: [
        ["#introduction", "Introduction"],
        ["#what-it-does", "What it does"],
        ["#submission-types", "Submission types"],
        ["#pipeline-workflow", "Workflow"],
      ],
    },
    {
      id: "install",
      file: "install.html",
      tab: "Install",
      links: [
        ["#prerequisites", "What you need"],
        ["#get-the-code", "Get the code"],
        ["#installation", "Run the application"],
        ["#verify", "Check it is running"],
        ["#tests", "Run the tests"],
      ],
    },
    {
      id: "how-it-works",
      file: "how-it-works.html",
      tab: "How it works",
      links: [
        ["#indexing", "Indexing"],
        ["#validation", "Validation"],
        ["#execution", "Execution"],
        ["#statistics", "Statistics"],
        ["#outputs", "Outputs"],
      ],
    },
    {
      id: "configuration",
      file: "configuration.html",
      tab: "Configuration",
      links: [
        ["#validation-rules", "Validation rules"],
        ["#dataset-structure", "Dataset structure"],
        ["#map-requirements", "Map requirements"],
        ["#filename-aliases", "Filename aliases"],
        ["#scoring-providers", "Scoring providers"],
        ["#reference-data", "Reference data and masks"],
        ["#apply-a-change", "Apply a change"],
      ],
    },
  ];

  // Hidden for now. `docs/status.html` still exists and still builds, but it
  // is absent from PAGES, so it appears in no navbar tab and no sidebar list
  // on any page. Restoring it means putting this entry back into PAGES above:
  //
  //   { id: "status", file: "status.html", tab: "Status",
  //     links: [["#scientific-status", "Scientific status"]] }
  //
  // The page sets `data-page="status"`, which no longer matches an entry, so
  // buildSidebar falls back to the Overview group. That is the intended
  // behaviour for an unlisted page and is why the fallback exists.

  const EXTERNAL = [
    ["https://osipi.github.io/", "OSIPI"],
    ["https://github.com/ralkhleef/osipi-perfusion-pipeline", "GitHub"],
  ];

  const current = document.body.dataset.page || "index";

  // ── Navbar tabs ────────────────────────────────────────────────────────
  const tabHost = document.querySelector("[data-navbar-tabs]");
  if (tabHost) {
    for (const page of PAGES) {
      const item = document.createElement("li");
      item.className = "nav-item";
      const link = document.createElement("a");
      link.className = "nav-link" + (page.id === current ? " active" : "");
      link.href = page.file;
      link.textContent = page.tab;
      if (page.id === current) link.setAttribute("aria-current", "page");
      item.appendChild(link);
      tabHost.appendChild(item);
    }
    for (const [href, label] of EXTERNAL) {
      const item = document.createElement("li");
      item.className = "nav-item d-none d-md-block";
      const link = document.createElement("a");
      link.className = "nav-link docs-nav-external";
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = label;
      item.appendChild(link);
      tabHost.appendChild(item);
    }
  }

  // ── Sidebar: this page's sections, then the other pages ────────────────
  function buildSidebar(host) {
    // An unlisted page — one deliberately kept out of PAGES — has no entry to
    // find. Falling back to the first page would print Overview's section
    // links under an Overview heading on a page that contains neither, so the
    // "on this page" group is skipped entirely and only the site map is shown.
    const page = PAGES.find((p) => p.id === current);

    if (page) {
      const onThis = document.createElement("div");
      onThis.className = "docs-nav-group";
      const heading = document.createElement("p");
      heading.className = "docs-nav-heading";
      heading.textContent = page.tab;
      onThis.appendChild(heading);
      for (const [href, label] of page.links) {
        const link = document.createElement("a");
        link.className = "docs-nav-link";
        link.href = href;
        link.textContent = label;
        onThis.appendChild(link);
      }
      host.appendChild(onThis);
    }

    // Sibling pages, so the sidebar is a map of the whole site rather than a
    // dead end at the bottom of one page.
    const others = PAGES.filter((p) => p.id !== current);
    if (others.length) {
      const group = document.createElement("div");
      group.className = "docs-nav-group";
      const title = document.createElement("p");
      title.className = "docs-nav-heading";
      title.textContent = "Other pages";
      group.appendChild(title);
      for (const other of others) {
        const link = document.createElement("a");
        link.className = "docs-nav-link";
        link.href = other.file;
        link.textContent = other.tab;
        group.appendChild(link);
      }
      host.appendChild(group);
    }
  }

  for (const id of ["sidebar-nav", "sidebar-nav-mobile"]) {
    const host = document.getElementById(id);
    if (host) buildSidebar(host);
  }
})();
