const navItems = document.querySelectorAll(".nav-item");
const panels = document.querySelectorAll(".panel");
const pageTitle = document.querySelector("#page-title");

function activatePanel(target) {
  navItems.forEach((item) => {
    item.classList.toggle("is-active", item.dataset.target === target);
  });

  panels.forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${target}`);
  });

  const activeItem = document.querySelector(`.nav-item[data-target="${target}"] span`);
  if (activeItem && pageTitle) {
    pageTitle.textContent = activeItem.textContent;
  }
}

navItems.forEach((item) => {
  item.addEventListener("click", () => activatePanel(item.dataset.target));
});
