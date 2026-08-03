(() => {
  const menu = document.querySelector('[data-menu]');
  const sidebar = document.querySelector('.sidebar');
  menu?.addEventListener('click', () => sidebar?.classList.toggle('open'));
  sidebar?.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', () => sidebar.classList.remove('open'));
  });

  const sections = [...document.querySelectorAll('main section[id]')];
  const navLinks = [...document.querySelectorAll('.sidebar a[href^="#"]')];
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    navLinks.forEach((link) => {
      link.classList.toggle('active', link.getAttribute('href') === `#${visible.target.id}`);
    });
  }, { rootMargin: '-15% 0px -70% 0px', threshold: [0, .25, .6] });
  sections.forEach((section) => observer.observe(section));

  document.querySelectorAll('.copy-button').forEach((button) => {
    button.addEventListener('click', async () => {
      const pre = button.closest('.code-title')?.nextElementSibling;
      if (!pre) return;
      try {
        await navigator.clipboard.writeText(pre.innerText);
        const old = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = old; }, 1200);
      } catch (_) {
        button.textContent = 'Select text';
      }
    });
  });

  const lightbox = document.querySelector('.lightbox');
  const lightboxImage = lightbox?.querySelector('img');
  document.querySelectorAll('.screenshot img').forEach((image) => {
    image.addEventListener('click', () => {
      if (!lightbox || !lightboxImage) return;
      lightboxImage.src = image.src;
      lightboxImage.alt = image.alt;
      lightbox.classList.add('open');
    });
  });
  const closeLightbox = () => lightbox?.classList.remove('open');
  lightbox?.addEventListener('click', closeLightbox);
  lightbox?.querySelector('button')?.addEventListener('click', closeLightbox);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeLightbox();
  });
})();
