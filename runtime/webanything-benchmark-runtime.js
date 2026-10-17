(function bootstrapWebAnythingRuntime(global) {
  "use strict";

  const clone = (value) => JSON.parse(JSON.stringify(value));

  function mulberry32(seed) {
    let value = seed >>> 0;
    return function next() {
      value = (value + 0x6d2b79f5) >>> 0;
      let mixed = value;
      mixed = Math.imul(mixed ^ (mixed >>> 15), mixed | 1);
      mixed ^= mixed + Math.imul(mixed ^ (mixed >>> 7), mixed | 61);
      return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
    };
  }

  function requireConfig(config) {
    if (!config || typeof config !== "object") throw new Error("benchmark runtime config is required");
    if (!/^webanything\.[a-z0-9][a-z0-9_]*\.v[1-9][0-9]*$/.test(config.namespace || "")) {
      throw new Error("invalid benchmark storage namespace");
    }
    if (!Number.isInteger(config.schemaVersion) || config.schemaVersion < 1) {
      throw new Error("invalid benchmark storage schema version");
    }
    if (typeof config.fixedTime !== "string" || !config.fixedTime.endsWith("Z")) {
      throw new Error("benchmark fixed time must be UTC");
    }
    if (!Number.isInteger(config.randomSeed)) throw new Error("benchmark random seed must be an integer");
    if (!config.initialState || typeof config.initialState !== "object") {
      throw new Error("benchmark initial state is required");
    }
    if (typeof config.initialUrl !== "string" || !config.initialUrl.startsWith("/")) {
      throw new Error("benchmark initial URL is required");
    }
  }

  function create(config) {
    requireConfig(config);
    const initialState = clone(config.initialState);
    const fixedEpochMs = Date.parse(config.fixedTime);
    if (!Number.isFinite(fixedEpochMs)) throw new Error("invalid benchmark fixed time");
    let random = mulberry32(config.randomSeed);

    function envelope(state) {
      return { schema_version: config.schemaVersion, state: clone(state) };
    }

    function writeState(state) {
      global.localStorage.setItem(config.namespace, JSON.stringify(envelope(state)));
      global.dispatchEvent(new CustomEvent("webanything:state", { detail: clone(state) }));
      return clone(state);
    }

    function readEnvelope() {
      const raw = global.localStorage.getItem(config.namespace);
      if (raw === null) return null;
      try {
        const parsed = JSON.parse(raw);
        if (
          !parsed || typeof parsed !== "object" ||
          parsed.schema_version !== config.schemaVersion ||
          !parsed.state || typeof parsed.state !== "object"
        ) return null;
        return parsed;
      } catch (_error) {
        return null;
      }
    }

    function getState() {
      const current = readEnvelope();
      if (current) return clone(current.state);
      return writeState(initialState);
    }

    function setState(nextState) {
      if (!nextState || typeof nextState !== "object") throw new Error("state must be an object");
      return writeState(nextState);
    }

    function update(mutator) {
      if (typeof mutator !== "function") throw new Error("state mutator must be a function");
      const draft = getState();
      const replacement = mutator(draft);
      return setState(replacement && typeof replacement === "object" ? replacement : draft);
    }

    function reset(options) {
      global.localStorage.removeItem(config.namespace);
      random = mulberry32(config.randomSeed);
      const state = writeState(initialState);
      const shouldNavigate = !options || options.navigate !== false;
      if (shouldNavigate) {
        const target = typeof config.resolveUrl === "function"
          ? config.resolveUrl(config.initialUrl)
          : config.initialUrl;
        if (global.location.pathname !== target) global.history.replaceState({}, "", target);
        global.dispatchEvent(new CustomEvent("webanything:route", { detail: { path: config.initialUrl } }));
      }
      return state;
    }

    function getClock() {
      return { iso: config.fixedTime, epoch_ms: fixedEpochMs, timezone: "UTC" };
    }

    function nextRandom() {
      return random();
    }

    const api = Object.freeze({
      namespace: config.namespace,
      schemaVersion: config.schemaVersion,
      getState,
      setState,
      update,
      reset,
      getClock,
      random: nextRandom,
    });
    if (!readEnvelope()) writeState(initialState);
    Object.defineProperty(global, "__WEBANYTHING__", {
      value: api,
      configurable: false,
      enumerable: true,
      writable: false,
    });
    return api;
  }

  Object.defineProperty(global, "WebAnythingRuntime", {
    value: Object.freeze({ create }),
    configurable: false,
    enumerable: true,
    writable: false,
  });
})(window);
