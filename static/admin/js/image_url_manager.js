/**
 * FLASHWEAR Admin Image URL Manager
 * 
 * Provides interactive inspection, candidate image selection from webpages,
 * and instant local file previews in Django admin forms and inlines.
 */

document.addEventListener("DOMContentLoaded", function () {
    const INSPECT_API_ENDPOINT = "/api/v1/catalog/admin/images/inspect-url/";

    function setupUrlInputs() {
        const urlInputs = document.querySelectorAll(
            'input[name$="source_url"], input[name$="hero_image_url"], input[name$="banner_image_url"], input[name$="image_url"], input[name$="logo_url"]'
        );

        urlInputs.forEach(function (input) {
            if (input.dataset.managerAttached) return;
            input.dataset.managerAttached = "true";

            // Create Inspect Button
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "image-url-inspect-btn";
            btn.textContent = "🔍 Inspect / Preview";
            input.parentNode.insertBefore(btn, input.nextSibling);

            // Create container for preview or candidates
            const container = document.createElement("div");
            container.className = "image-url-preview-container";
            container.style.display = "none";
            btn.parentNode.insertBefore(container, btn.nextSibling);

            btn.addEventListener("click", function () {
                const url = input.value.trim();
                if (!url) {
                    showError(container, "Please enter an image or webpage URL first.");
                    return;
                }

                showLoading(container);

                fetch(`${INSPECT_API_ENDPOINT}?url=${encodeURIComponent(url)}`, {
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                    },
                })
                    .then(function (response) {
                        return response.json().then(function (data) {
                            if (!response.ok) {
                                throw new Error(data.error || `HTTP error ${response.status}`);
                            }
                            return data;
                        });
                    })
                    .then(function (data) {
                        renderInspectionResult(data, input, container);
                    })
                    .catch(function (err) {
                        showError(container, err.message || "Failed to inspect URL.");
                    });
            });
        });
    }

    function showLoading(container) {
        container.style.display = "block";
        container.innerHTML = '<span style="color:#6b7280;">⏳ Validating URL and inspecting resource safely...</span>';
    }

    function showError(container, message) {
        container.style.display = "block";
        container.innerHTML = `<div class="image-url-error-msg">⚠️ ${escapeHtml(message)}</div>`;
    }

    function renderInspectionResult(data, input, container) {
        container.style.display = "block";

        if (data.type === "image") {
            container.innerHTML = `
                <div class="image-url-preview-card">
                    <img class="image-url-preview-thumb" src="${escapeHtml(data.url)}" alt="Preview" onerror="this.style.display='none'" />
                    <div class="image-url-preview-meta">
                        <span class="meta-tag">Direct Image</span>
                        <div><strong>Format:</strong> ${escapeHtml(data.format || "Image")}</div>
                        <div><strong>Dimensions:</strong> ${data.width || "?"} × ${data.height || "?"} px</div>
                        <div><strong>Size:</strong> ${(data.size / 1024).toFixed(1)} KB</div>
                    </div>
                </div>
            `;
        } else if (data.type === "webpage") {
            if (data.error || !data.candidates || data.candidates.length === 0) {
                container.innerHTML = `
                    <div class="image-url-error-msg">
                        ⚠️ ${escapeHtml(data.error || "This URL is a webpage, not a directly accessible image. Please provide a direct image URL.")}
                    </div>
                `;
                return;
            }

            let html = `
                <div class="image-url-candidates-drawer">
                    <div style="font-weight:600;margin-bottom:4px;">🌐 Webpage: ${escapeHtml(data.title || "Untitled")}</div>
                    <div style="font-size:12px;color:#6b7280;margin-bottom:8px;">Found ${data.candidates.length} candidate image(s). Click one to select:</div>
                    <div class="image-url-candidates-grid">
            `;

            data.candidates.forEach(function (cand, idx) {
                html += `
                    <div class="image-url-candidate-item" data-url="${escapeHtml(cand.url)}" data-alt="${escapeHtml(cand.alt || '')}">
                        <img src="${escapeHtml(cand.url)}" alt="${escapeHtml(cand.alt || 'Candidate ' + (idx + 1))}" />
                    </div>
                `;
            });

            html += `</div></div>`;
            container.innerHTML = html;

            // Add selection listener
            const items = container.querySelectorAll(".image-url-candidate-item");
            items.forEach(function (item) {
                item.addEventListener("click", function () {
                    items.forEach(i => i.classList.remove("selected"));
                    item.classList.add("selected");

                    const chosenUrl = item.dataset.url;
                    const chosenAlt = item.dataset.alt;

                    // Locate candidate_image_url hidden input in the same form row if present
                    const parentForm = input.closest("tr, fieldset, form");
                    if (parentForm) {
                        const candidateInput = parentForm.querySelector('input[name$="candidate_image_url"]');
                        if (candidateInput) {
                            candidateInput.value = chosenUrl;
                        }
                        const altInput = parentForm.querySelector('input[name$="alt_text"]');
                        if (altInput && !altInput.value && chosenAlt) {
                            altInput.value = chosenAlt;
                        }
                    }

                    // Render selected confirmation
                    const previewThumb = document.createElement("div");
                    previewThumb.style.marginTop = "8px";
                    previewThumb.innerHTML = `
                        <div style="color:#065f46;background:#d1fae5;padding:6px 10px;border-radius:4px;font-size:12px;display:flex;align-items:center;gap:6px;">
                            <span>✓ Selected candidate image</span>
                        </div>
                    `;
                    container.appendChild(previewThumb);
                });
            });
        }
    }

    function setupFileInputPreviews() {
        const fileInputs = document.querySelectorAll('input[type="file"][name$="image"], input[type="file"][name$="hero_image"], input[type="file"][name$="banner_image"], input[type="file"][name$="logo"]');
        fileInputs.forEach(function (input) {
            if (input.dataset.previewAttached) return;
            input.dataset.previewAttached = "true";

            input.addEventListener("change", function () {
                const file = input.files && input.files[0];
                let existingPreview = input.parentNode.querySelector(".local-file-preview");
                if (existingPreview) existingPreview.remove();

                if (file && file.type.startsWith("image/")) {
                    const objectUrl = URL.createObjectURL(file);
                    const previewDiv = document.createElement("div");
                    previewDiv.className = "local-file-preview";
                    previewDiv.style.marginTop = "6px";
                    previewDiv.innerHTML = `
                        <div style="display:flex;align-items:center;gap:8px;">
                            <img src="${objectUrl}" style="height:60px;width:60px;object-fit:cover;border-radius:6px;border:1px solid #d1d5db;" alt="Upload Preview" />
                            <span style="font-size:12px;color:#374151;">Selected: <strong>${escapeHtml(file.name)}</strong> (${(file.size / 1024).toFixed(1)} KB)</span>
                        </div>
                    `;
                    input.parentNode.appendChild(previewDiv);
                }
            });
        });
    }

    function escapeHtml(str) {
        if (!str) return "";
        return str
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    setupUrlInputs();
    setupFileInputPreviews();

    // Listen for dynamically added formset rows in Django TabularInline
    document.addEventListener("formset:added", function () {
        setupUrlInputs();
        setupFileInputPreviews();
    });
});
