/* Pinterest base tag. Keep this loaded once per page, before any event tags. */
!function(e){if(!window.pintrk){window.pintrk=function(){window.pintrk.queue.push(Array.prototype.slice.call(arguments))};var
n=window.pintrk;n.queue=[];n.version="3.0";var
t=document.createElement("script");t.async=!0;t.src=e;var
r=document.getElementsByTagName("script")[0];r.parentNode.insertBefore(t,r)}}("https://s.pinimg.com/ct/core.js");
pintrk("load", "2612904762430");
pintrk("page");

function pinterestEventId(prefix) {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return prefix + "_" + window.crypto.randomUUID();
  }
  return prefix + "_" + Date.now() + "_" + Math.random().toString(16).slice(2);
}

document.addEventListener("click", function(event) {
  var link = event.target.closest && event.target.closest("a[href]");
  if (!link || typeof window.pintrk !== "function") return;

  if (link.href.indexOf("https://discord.com/oauth2/authorize") === 0) {
    pintrk("track", "signup", {
      event_id: pinterestEventId("owaua_invite"),
      lead_type: "bot_invite"
    });
  } else if (link.href.indexOf("https://discord.gg/") === 0) {
    pintrk("track", "lead", {
      event_id: pinterestEventId("owaua_discord"),
      lead_type: "discord_community"
    });
  }
});
