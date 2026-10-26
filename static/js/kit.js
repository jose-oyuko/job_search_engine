// Copy buttons for the kit panel: <button data-copy="#element-id">. Works on swapped-in HTML.
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var source = document.querySelector(button.getAttribute("data-copy"));
  if (!source || !navigator.clipboard) return;
  var label = button.textContent;
  navigator.clipboard.writeText(source.innerText.trim()).then(function () {
    button.textContent = "Copied";
    setTimeout(function () { button.textContent = label; }, 1500);
  });
});
