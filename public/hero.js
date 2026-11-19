// Adds the Maskan hero (headline, intro, key numbers) to Chainlit's empty home screen.
(function () {
  const HERO = `
    <div class="eyebrow">Dubai · Riyadh · Jeddah · Muscat · London</div>
    <h1>Your next address, <em>curated.</em></h1>
    <p class="lead">From branded towers on the Dubai Canal to seafront villas in Oman and family homes
      in Riyadh and Jeddah: ask in English or Arabic, and get answers grounded in real listings,
      with every source linked.</p>
    <div class="stats">
      <div class="stat"><b>38</b><span>DarGlobal projects</span></div>
      <div class="stat"><b>6</b><span>Countries</span></div>
      <div class="stat"><b>2</b><span>Cities on Wasalt</span></div>
    </div>
    <div class="rule"></div>`;

  function mount() {
    const screen = document.getElementById("welcome-screen");
    if (!screen || document.getElementById("maskan-hero")) return;
    const hero = document.createElement("div");
    hero.id = "maskan-hero";
    hero.innerHTML = HERO;
    const logo = screen.querySelector("img");
    if (logo && logo.parentElement === screen) logo.after(hero);
    else screen.prepend(hero);
  }

  new MutationObserver(mount).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", mount);
})();
