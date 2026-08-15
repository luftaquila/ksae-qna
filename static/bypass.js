const runWhenReady = (callback) => {
  if (document.readyState !== "loading") {
    callback();
  } else {
    document.addEventListener("DOMContentLoaded", callback);
  }
};

runWhenReady(() => {
  if (/kakaotalk/i.test(navigator.userAgent)) {
    location.href = "kakaotalk://web/openExternal?url=" + encodeURIComponent(location.href);
  }
});
