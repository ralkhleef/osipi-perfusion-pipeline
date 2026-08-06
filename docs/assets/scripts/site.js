/* Documentation site behaviour: on-this-page navigation, scroll tracking,
 * copy buttons.
 *
 * No build step and no framework — Bootstrap's own JS bundle handles the
 * offcanvas menu; everything below is plain DOM work.
 */

(() => {
  "use strict";

  const content = document.querySelector(".docs-content");
  if (!content) return;

  // ── On this page ───────────────────────────────────────────────────────
  // Built from the headings that are actually present, so the list can never
  // drift from the document the way a hand-maintained one would.
  const headings = Array.from(content.querySelectorAll("h2[id], h3[id]"));
  const toc = document.querySelector("[data-toc]");

  if (toc && headings.length) {
    for (const heading of headings) {
      const link = document.createElement("a");
      link.href = `#${heading.id}`;
      link.textContent = heading.dataset.tocTitle || heading.textContent.trim();
      if (heading.tagName === "H3") link.style.paddingLeft = "1.5rem";
      toc.appendChild(link);
    }
  }

  // ── Heading anchors ────────────────────────────────────────────────────
  for (const heading of headings) {
    const anchor = document.createElement("a");
    anchor.className = "docs-anchor";
    anchor.href = `#${heading.id}`;
    anchor.setAttribute("aria-label", `Link to ${heading.textContent.trim()}`);
    anchor.textContent = "#";
    heading.appendChild(anchor);
  }

  // ── Active-section tracking ────────────────────────────────────────────
  // IntersectionObserver alone marks a section active as soon as any part of
  // it is visible, which flickers between neighbours on a fast scroll. Taking
  // the last heading above the reading line is stable and matches what the
  // reader is actually looking at.
  const sidebarLinks = Array.from(document.querySelectorAll(".docs-nav-link[href^='#']"));
  const tocLinks = () => Array.from(document.querySelectorAll(".docs-toc a"));

  // Track whatever the sidebar actually points at. Some entries target a
  // heading inside a section rather than the section itself, and matching on
  // sections alone left those links permanently unhighlighted.
  const targets = sidebarLinks
    .map((link) => document.getElementById(link.getAttribute("href").slice(1)))
    .filter(Boolean)
    .sort((a, b) => a.offsetTop - b.offsetTop);

  function setActive() {
    const line = window.scrollY + window.innerHeight * 0.25;

    let currentTarget = targets[0];
    for (const target of targets) {
      if (target.offsetTop <= line) currentTarget = target;
    }
    let currentHeading = headings[0];
    for (const heading of headings) {
      if (heading.offsetTop <= line) currentHeading = heading;
    }

    for (const link of sidebarLinks) {
      const active = currentTarget && link.getAttribute("href") === `#${currentTarget.id}`;
      link.classList.toggle("active", Boolean(active));
      if (active) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    }
    for (const link of tocLinks()) {
      link.classList.toggle(
        "active",
        Boolean(currentHeading) && link.getAttribute("href") === `#${currentHeading.id}`
      );
    }
  }

  let ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(() => { setActive(); ticking = false; });
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  setActive();

  // Close the mobile menu after a jump, or the target is hidden behind it.
  document.addEventListener("click", (event) => {
    const link = event.target.closest(".offcanvas .docs-nav-link");
    if (!link) return;
    const panel = link.closest(".offcanvas");
    const instance = window.bootstrap && window.bootstrap.Offcanvas.getInstance(panel);
    if (instance) instance.hide();
  });

  // ── Copy buttons ───────────────────────────────────────────────────────
  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".docs-copy");
    if (!button) return;
    const block = button.closest(".docs-code");
    const code = block && block.querySelector("pre");
    if (!code) return;

    const label = button.textContent;
    try {
      await navigator.clipboard.writeText(code.innerText);
      button.textContent = "Copied";
      button.classList.add("copied");
    } catch {
      // Clipboard access is refused on insecure origins and in some browsers;
      // select the text so the reader can copy it manually rather than
      // leaving the button looking broken.
      const range = document.createRange();
      range.selectNodeContents(code);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = "Press ⌘C";
    }
    setTimeout(() => {
      button.textContent = label;
      button.classList.remove("copied");
    }, 2000);
  });

})();
