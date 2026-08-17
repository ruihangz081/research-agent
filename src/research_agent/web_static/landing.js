(() => {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const hero = document.querySelector(".landing-hero");
  const parallaxItems = hero ? Array.from(hero.querySelectorAll("[data-parallax-depth]")) : [];
  const rippleField = hero?.querySelector(".hero-ripple-field");
  const flow = document.querySelector(".hero-flow");
  const flowNodes = flow ? Array.from(flow.querySelectorAll(":scope > span")) : [];
  let flowTimer = 0;
  let activeFlowIndex = -1;
  let parallaxFrame = 0;
  let targetX = 0;
  let targetY = 0;
  let currentX = 0;
  let currentY = 0;
  let targetFieldX = 0;
  let targetFieldY = 0;
  let currentFieldX = 0;
  let currentFieldY = 0;
  let fieldInitialized = false;
  let rippleIndex = 0;
  let lastRippleTime = 0;
  let lastRippleX = 0;
  let lastRippleY = 0;
  let rippleOriginSet = false;

  const ripplePool = rippleField ? Array.from({ length: 7 }, () => {
    const ripple = document.createElement("i");
    ripple.className = "hero-ripple";
    rippleField.append(ripple);
    return ripple;
  }) : [];

  const flowDot = document.createElement("b");
  flowDot.className = "flow-energy-dot";
  flowDot.setAttribute("aria-hidden", "true");
  flow?.append(flowDot);

  function setFlowNode(index, immediate = false) {
    const node = flowNodes[index];
    if (!flow || !node) return;
    const flowRect = flow.getBoundingClientRect();
    const nodeRect = node.getBoundingClientRect();
    flowDot.classList.toggle("is-immediate", immediate);
    flowDot.style.setProperty("--flow-x", `${nodeRect.left - flowRect.left + nodeRect.width / 2}px`);
    flowDot.style.setProperty("--flow-y", `${nodeRect.top - flowRect.top + nodeRect.height / 2}px`);
    flowDot.classList.add("is-visible");
    flowNodes.forEach((item, itemIndex) => item.classList.toggle("is-active", itemIndex === index));
  }

  function stopFlow() {
    window.clearTimeout(flowTimer);
    flowNodes.forEach((item) => item.classList.remove("is-active"));
    flowDot.classList.remove("is-visible");
    activeFlowIndex = -1;
  }

  function advanceFlow() {
    if (document.hidden || reducedMotion.matches) return;
    if (activeFlowIndex < flowNodes.length - 1) {
      activeFlowIndex += 1;
      setFlowNode(activeFlowIndex, activeFlowIndex === 0);
      flowTimer = window.setTimeout(advanceFlow, 760);
      return;
    }
    flowTimer = window.setTimeout(() => {
      stopFlow();
      flowTimer = window.setTimeout(advanceFlow, 1400);
    }, 620);
  }

  function paintParallax() {
    currentX += (targetX - currentX) * 0.085;
    currentY += (targetY - currentY) * 0.085;
    currentFieldX += (targetFieldX - currentFieldX) * 0.065;
    currentFieldY += (targetFieldY - currentFieldY) * 0.065;
    parallaxItems.forEach((item) => {
      const depth = Number(item.dataset.parallaxDepth) || 0;
      item.style.setProperty("--parallax-x", `${(currentX * depth).toFixed(2)}px`);
      item.style.setProperty("--parallax-y", `${(currentY * depth).toFixed(2)}px`);
    });
    if (fieldInitialized) {
      hero?.style.setProperty("--field-x", `${currentFieldX.toFixed(2)}px`);
      hero?.style.setProperty("--field-y", `${currentFieldY.toFixed(2)}px`);
    }
    if (Math.abs(targetX - currentX) > 0.02 || Math.abs(targetY - currentY) > 0.02 || Math.abs(targetFieldX - currentFieldX) > 0.08 || Math.abs(targetFieldY - currentFieldY) > 0.08) {
      parallaxFrame = window.requestAnimationFrame(paintParallax);
    } else {
      parallaxFrame = 0;
    }
  }

  function requestParallaxFrame() {
    if (!parallaxFrame) parallaxFrame = window.requestAnimationFrame(paintParallax);
  }

  function emitRipple(x, y, timestamp) {
    if (!ripplePool.length || reducedMotion.matches) return;
    const deltaX = x - lastRippleX;
    const deltaY = y - lastRippleY;
    const distance = Math.hypot(deltaX, deltaY);
    if (rippleOriginSet && (timestamp - lastRippleTime < 150 || distance < 42)) return;

    const ripple = ripplePool[rippleIndex];
    const angle = rippleOriginSet ? Math.max(-7, Math.min(7, Math.atan2(deltaY, deltaX) * 2.4)) : 0;
    const size = 168 + Math.min(distance, 110) * 0.42 + (rippleIndex % 3) * 10;
    rippleIndex = (rippleIndex + 1) % ripplePool.length;
    lastRippleTime = timestamp;
    lastRippleX = x;
    lastRippleY = y;
    rippleOriginSet = true;

    ripple.style.setProperty("--ripple-x", `${x.toFixed(1)}px`);
    ripple.style.setProperty("--ripple-y", `${y.toFixed(1)}px`);
    ripple.style.setProperty("--ripple-size", `${size.toFixed(1)}px`);
    ripple.getAnimations().forEach((animation) => animation.cancel());
    ripple.animate([
      { transform: `translate3d(-50%, -50%, 0) rotate(${angle.toFixed(2)}deg) scale(.28)`, opacity: 0 },
      { opacity: .32, offset: .16 },
      { transform: `translate3d(-50%, -50%, 0) rotate(${angle.toFixed(2)}deg) scale(1)`, opacity: 0 }
    ], {
      duration: 1550,
      easing: "cubic-bezier(.16,.68,.26,1)",
      fill: "forwards"
    });
  }

  function clearRipples() {
    ripplePool.forEach((ripple) => {
      ripple.getAnimations().forEach((animation) => animation.cancel());
      ripple.style.opacity = "0";
    });
    rippleOriginSet = false;
  }

  function updateParallax(event) {
    if (!hero || reducedMotion.matches || event.pointerType === "touch") return;
    const rect = hero.getBoundingClientRect();
    targetX = ((event.clientX - rect.left) / rect.width - 0.5) * 24;
    targetY = ((event.clientY - rect.top) / rect.height - 0.5) * 20;
    targetFieldX = event.clientX - rect.left;
    targetFieldY = event.clientY - rect.top;
    if (!fieldInitialized) {
      currentFieldX = targetFieldX;
      currentFieldY = targetFieldY;
      fieldInitialized = true;
    }
    hero.classList.add("field-active");
    emitRipple(targetFieldX, targetFieldY, event.timeStamp);
    requestParallaxFrame();
  }

  function resetParallax() {
    targetX = 0;
    targetY = 0;
    hero?.classList.remove("field-active");
    if (hero) {
      const rect = hero.getBoundingClientRect();
      targetFieldX = rect.width / 2;
      targetFieldY = rect.height * 0.42;
    }
    requestParallaxFrame();
  }

  function syncMotionPreference() {
    if (reducedMotion.matches) {
      stopFlow();
      resetParallax();
      hero?.classList.remove("field-active");
      clearRipples();
    } else if (!document.hidden && flowNodes.length) {
      stopFlow();
      flowTimer = window.setTimeout(advanceFlow, 1200);
    }
  }

  hero?.addEventListener("pointermove", updateParallax, { passive: true });
  hero?.addEventListener("pointerleave", resetParallax, { passive: true });
  window.addEventListener("resize", () => {
    if (activeFlowIndex >= 0) setFlowNode(activeFlowIndex, true);
  }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    document.documentElement.classList.toggle("motion-paused", document.hidden);
    if (document.hidden) {
      stopFlow();
      hero?.classList.remove("field-active");
      clearRipples();
    }
    else syncMotionPreference();
  });
  reducedMotion.addEventListener("change", syncMotionPreference);

  window.requestAnimationFrame(() => {
    document.documentElement.classList.add("motion-ready");
    syncMotionPreference();
  });
})();
