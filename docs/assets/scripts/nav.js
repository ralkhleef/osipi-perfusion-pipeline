/* Shared navigation for the static documentation pages. */

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
        ["#submission-workflows", "Submission workflows"],
        ["#pipeline-workflow", "Workflow"],
      ],
    },
    {
      id: "install",
      file: "install.html",
      tab: "Install",
      links: [
        ["#prerequisites", "Requirements"],
        ["#get-the-code", "Getting the code"],
        ["#installation", "Running the application"],
        ["#verify", "Verifying the install"],
        ["#tests", "Running the tests"],
      ],
    },
    {
      id: "how-it-works",
      file: "how-it-works.html",
      tab: "How it works",
      links: [
        ["#why", "Background"],
        ["#the-steps", "Pipeline overview"],
        ["#indexing", "Upload and Review"],
        ["#validation", "Validate"],
        ["#execution", "Run"],
        ["#statistics", "QC and Preview"],
        ["#outputs", "Export"],
      ],
    },
    {
      id: "examples",
      file: "examples.html",
      tab: "Examples",
      links: [
        ["#interface", "The interface"],
        ["#example-output", "Example output"],
      ],
    },
    {
      id: "configuration",
      file: "configuration.html",
      tab: "Configuration",
      links: [
        ["#configuration-manager", "In-app manager"],
        ["#validation-rules", "Validation rules"],
        ["#dataset-structure", "Dataset structure"],
        ["#map-requirements", "Map requirements"],
        ["#filename-aliases", "Filename aliases"],
        ["#scoring-providers", "Scoring providers"],
        ["#reference-data", "Reference data and masks"],
        ["#apply-a-change", "Apply a change"],
      ],
    },
    {
      id: "gsoc",
      file: "gsoc.html",
      tab: "GSoC",
      links: [
        ["#project", "The project"],
        ["#challenge-review", "Built for challenge review"],
        ["#how-built", "How it was built"],
        ["#testing", "Testing and reliability"],
        ["#result", "Project result"],
        ["#documents", "Documents"],
      ],
    },
    {
      id: "pending-requirements",
      file: "pending-requirements.html",
      tab: "Pending",
      links: [
        ["#purpose", "Purpose"],
        ["#dce", "DCE"],
        ["#asl", "ASL"],
        ["#dsc", "DSC"],
        ["#shared", "Shared decisions"],
      ],
    },
  ];

  // status.html is available by direct link but is not shown in the menu.

  const EXTERNAL = [
    ["https://osipi.ismrm.org/", "OSIPI"],
    ["https://github.com/ralkhleef/osipi-perfusion-pipeline", "GitHub"],
  ];

  const current = document.body.dataset.page || "index";

  // Navbar
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

  // Sidebar
  function buildSidebar(host) {
    // Unlisted pages show only links to the other pages.
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

    // Links to the rest of the site.
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
