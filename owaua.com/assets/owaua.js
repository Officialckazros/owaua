'use strict';

const menu = document.querySelector('.menu');
if (menu) {
  menu.addEventListener('click', event => {
    if (event.target.closest('a')) menu.open = false;
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.open) {
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });
  document.addEventListener('click', event => {
    if (!menu.contains(event.target)) menu.open = false;
  });
}

const contactLinks = document.querySelectorAll('.contact-link[data-contact-email]');
if (contactLinks.length) {
  const status = document.createElement('div');
  status.className = 'contact-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  document.body.append(status);

  let statusTimer;
  for (const link of contactLinks) {
    link.addEventListener('click', () => {
      const email = link.dataset.contactEmail;
      status.textContent = `Email address: ${email}`;
      status.classList.add('visible');
      clearTimeout(statusTimer);
      statusTimer = setTimeout(() => status.classList.remove('visible'), 7000);
      if (!navigator.clipboard?.writeText) return;
      navigator.clipboard.writeText(email).then(() => {
        status.textContent = `Email address copied: ${email}`;
      }).catch(() => {});
    });
  }
}
