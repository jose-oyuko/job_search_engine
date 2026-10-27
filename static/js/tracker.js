// Drag and drop for the tracker board. A drop posts the new stage; the server answers with the
// whole board, which replaces this one (see tracker/_board.html), so the counts stay right.
(function () {
  function init() {
    if (!window.Sortable) return;
    document.querySelectorAll(".stage-list").forEach(function (list) {
      if (list.dataset.sortable) return;
      list.dataset.sortable = "1";
      new Sortable(list, {
        group: "applications",
        animation: 120,
        ghostClass: "opacity-50",
        // On a phone a finger drag also scrolls the page: press and hold briefly to pick a card
        // up. The mouse on a PC stays instant. The stage dropdown never starts a drag.
        delay: 200,
        delayOnTouchOnly: true,
        touchStartThreshold: 5,
        filter: "select, option, a",
        preventOnFilter: false,
        onEnd: function (event) {
          var to = event.to.dataset.stage;
          if (event.from === event.to) return;
          window.htmx.ajax("POST", event.item.dataset.stageUrl, {
            target: "#board",
            swap: "outerHTML",
            values: { stage: to },
          });
        },
      });
    });
  }
  document.addEventListener("DOMContentLoaded", init);
  document.body.addEventListener("htmx:afterSettle", init);
  // If a move fails, put the board back as the server has it.
  document.body.addEventListener("htmx:responseError", function () { window.location.reload(); });
})();
