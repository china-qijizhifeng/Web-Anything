(async function resetCloudPost() {
  "use strict";

  function sitePrefix() {
    const tokens = window.location.pathname.split("/").filter(Boolean);
    if (tokens[0] === "p" && /^\d+$/.test(tokens[1] || "")) {
      return ["", tokens[0], tokens[1]].join("/");
    }
    const siteIndex = tokens.indexOf("sites");
    if (siteIndex >= 0 && tokens[siteIndex + 1] === "cloudpost") {
      return "/" + tokens.slice(0, siteIndex + 2).join("/");
    }
    return "";
  }

  function resolveUrl(path) {
    return sitePrefix() + path;
  }

  try {
    const [contract, initialState] = await Promise.all([
      fetch(resolveUrl("/synthetic_contract.json")).then(response => response.json()),
      fetch(resolveUrl("/fixtures/initial-state.json")).then(response => response.json())
    ]);
    const runtime = window.WebAnythingRuntime.create({
      namespace: contract.storage.namespace,
      schemaVersion: contract.storage.schema_version,
      fixedTime: contract.clock.fixed_time,
      randomSeed: contract.random.seed,
      initialState,
      initialUrl: contract.reset.initial_url,
      resolveUrl
    });
    runtime.reset({ navigate: false });
    window.location.replace(resolveUrl(contract.reset.initial_url));
  } catch (error) {
    const card = document.querySelector("[data-reset-status]");
    card.dataset.resetStatus = "failed";
    card.querySelector("h1").textContent = "Reset failed";
    card.querySelector("p").textContent = error.message;
  }
})();
