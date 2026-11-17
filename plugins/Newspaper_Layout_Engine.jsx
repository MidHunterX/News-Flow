(function () {
  // =========================================================
  // CONFIGURATION
  // =========================================================

  var BASE_URL = "http://192.168.1.34:8693";
  var API_PATH = "/api/publication/export?day=previous";

  var STYLE_HEADING = "Heading";
  var STYLE_COVER = "Cover Picture";
  var STYLE_BODY = "Body 4 Col";

  // Whether to bring each page into view before placing articles
  var ACTIVATE_PAGE = true;

  // HTTP timeout in seconds
  var HTTP_TIMEOUT = 30;

  // =========================================================
  // LOGGING
  // =========================================================

  function log(message) {
    $.writeln("[Publication Import] " + message);
  }

  // =========================================================
  // HTTP UTILITIES
  // =========================================================

  function parseURL(url) {
    var match = url.match(/^http:\/\/([^\/:]+)(?::(\d+))?(\/.*)?$/i);
    if (!match) {
      throw new Error("Invalid HTTP URL: " + url);
    }
    return {
      host: match[1],
      port: match[2] ? Number(match[2]) : 80,
      path: match[3] || "/",
    };
  }

  function decodeChunked(body) {
    var result = "";
    var position = 0;
    while (position < body.length) {
      var lineEnd = body.indexOf("\r\n", position);
      if (lineEnd < 0) {
        break;
      }
      var sizeText = body.substring(position, lineEnd);
      // Remove optional chunk extensions
      sizeText = sizeText.split(";")[0];
      var chunkSize = parseInt(sizeText, 16);
      if (isNaN(chunkSize)) {
        throw new Error("Invalid HTTP chunk.");
      }
      if (chunkSize === 0) {
        break;
      }
      position = lineEnd + 2;
      result += body.substr(position, chunkSize);
      position += chunkSize + 2;
    }
    return result;
  }

  function httpGet(url) {
    url = encodeURI(url);
    var parsed = parseURL(url);
    var socket = new Socket();
    socket.timeout = HTTP_TIMEOUT;
    socket.encoding = "BINARY";
    log("Connecting: " + url);
    if (!socket.open(parsed.host + ":" + parsed.port, "BINARY")) {
      throw new Error(
        "Could not connect to " +
          parsed.host +
          ":" +
          parsed.port +
          "\n" +
          socket.error,
      );
    }
    var request =
      "GET " +
      parsed.path +
      " HTTP/1.1\r\n" +
      "Host: " +
      parsed.host +
      ":" +
      parsed.port +
      "\r\n" +
      "Connection: close\r\n" +
      "Accept: application/json, image/*, */*\r\n" +
      "Accept-Encoding: identity\r\n" +
      "User-Agent: InDesign-ExtendScript\r\n" +
      "\r\n";
    socket.write(request);
    var response = "";
    var headerEnd = -1;
    var expectedLength = -1;
    try {
      while (socket.connected) {
        var chunk = socket.read(8192);
        if (!chunk || chunk.length === 0) {
          break;
        }
        response += chunk;
        if (headerEnd < 0) {
          headerEnd = response.indexOf("\r\n\r\n");
          if (headerEnd >= 0) {
            var headerText = response.substring(0, headerEnd);
            var lengthMatch = headerText.match(/Content-Length:\s*(\d+)/i);
            if (lengthMatch) {
              expectedLength = Number(lengthMatch[1]);
            }
          }
        }
        // Stop when the complete Content-Length body arrives
        if (headerEnd >= 0 && expectedLength >= 0) {
          var receivedLength = response.length - headerEnd - 4;
          if (receivedLength >= expectedLength) {
            break;
          }
        }
      }
    } finally {
      socket.close();
    }
    if (headerEnd < 0) {
      throw new Error("Invalid HTTP response.");
    }
    var headers = response.substring(0, headerEnd);
    var body = response.substring(headerEnd + 4);
    var statusMatch = headers.match(/^HTTP\/\d(?:\.\d)?\s+(\d+)/i);
    if (!statusMatch) {
      throw new Error("Could not read HTTP status.");
    }
    var status = Number(statusMatch[1]);
    if (status < 200 || status >= 300) {
      throw new Error("HTTP error " + status + "\n" + body);
    }
    // Decode chunked transfer encoding
    if (/Transfer-Encoding:\s*chunked/i.test(headers)) {
      body = decodeChunked(body);
    }
    // Verify Content-Length where supplied
    if (expectedLength >= 0 && body.length < expectedLength) {
      throw new Error(
        "Incomplete HTTP response. Expected " +
          expectedLength +
          " bytes, received " +
          body.length +
          ".",
      );
    }
    return {
      headers: headers,
      body: body,
    };
  }

  // =========================================================
  // JSON UTILITIES
  // =========================================================

  function decodeUTF8(binaryString) {
    // Convert UTF-8 bytes to a JavaScript Unicode string
    return decodeURIComponent(escape(binaryString));
  }

  function fetchPublicationData() {
    var url = BASE_URL.replace(/\/+$/, "") + API_PATH;
    log("Fetching publication JSON...");
    var response = httpGet(url);
    var jsonText = decodeUTF8(response.body);
    // Remove UTF-8 BOM if present
    jsonText = jsonText.replace(/^\uFEFF/, "");
    var data;
    try {
      data = eval("(" + jsonText + ")");
    } catch (e) {
      throw new Error("Could not parse API JSON:\n" + e.message);
    }
    if (!data.pages || !data.pages.length) {
      throw new Error("API returned no publication pages.");
    }
    log("JSON received. Pages: " + data.pages.length);
    return data;
  }

  // =========================================================
  // IMAGE DOWNLOAD
  // =========================================================

  function resolveImageURL(path) {
    if (/^https?:\/\//i.test(path)) {
      return path;
    }
    return BASE_URL.replace(/\/+$/, "") + "/" + path.replace(/^\/+/, "");
  }

  function getImageExtension(url) {
    var cleanURL = url.split("?")[0];
    var match = cleanURL.match(/\.(jpg|jpeg|png|gif|tif|tiff|psd)$/i);
    return match ? match[1].toLowerCase() : "jpg";
  }

  function downloadImage(url, articleID) {
    var response = httpGet(url);
    var extension = getImageExtension(url);
    var folder = new Folder(Folder.temp.fsName + "/PublicationCovers");
    if (!folder.exists && !folder.create()) {
      throw new Error("Could not create temporary image folder.");
    }
    var file = new File(
      folder.fsName + "/article_" + articleID + "." + extension,
    );
    // Write raw binary image data
    file.encoding = "BINARY";
    if (!file.open("w")) {
      throw new Error("Could not write image: " + file.fsName);
    }
    try {
      file.write(response.body);
    } finally {
      file.close();
    }
    if (!file.exists || file.length === 0) {
      throw new Error("Downloaded image is empty: " + url);
    }
    log("Downloaded image: " + url);
    return file;
  }

  // =========================================================
  // INDESIGN OBJECT STYLES
  // =========================================================

  function getObjectStyle(name) {
    var style = app.activeDocument.objectStyles.itemByName(name);
    if (!style.isValid) {
      throw new Error("Object style not found: " + name);
    }
    return style;
  }

  // =========================================================
  // FRAME CREATION
  // =========================================================

  function createTextFrame(page, bounds, content, objectStyle, paragraphStyle) {
    var frame = page.textFrames.add();
    frame.geometricBounds = bounds;
    frame.contents = content || "";
    frame.applyObjectStyle(objectStyle, true);
    applyParagraphStyle(frame, paragraphStyle);
    return frame;
  }

  function createImageFrame(page, bounds, imageFile, objectStyle) {
    var frame = page.rectangles.add();
    frame.geometricBounds = bounds;
    frame.place(imageFile);
    frame.applyObjectStyle(objectStyle, true);
    return frame;
  }

  // =========================================================
  // PAGE UTILITIES
  // =========================================================

  function getPage(pageNumber, doc) {
    if (!pageNumber || pageNumber < 1) {
      throw new Error("Invalid page number: " + pageNumber);
    }
    while (doc.pages.length < pageNumber) {
      doc.pages.add();
    }
    return doc.pages.item(pageNumber - 1);
  }

  function getPageLayout(page) {
    var bounds = page.bounds;
    var margins = page.marginPreferences;
    return {
      top: bounds[0] + margins.top,
      left: bounds[1] + margins.left,
      bottom: bounds[2] - margins.bottom,
      right: bounds[3] - margins.right,
    };
  }

  // =========================================================
  // FOUR-COLUMN NEWSPAPER LAYOUT ENGINE
  // =========================================================

  // Layout configuration
  var EXPECTED_COLUMNS = 4;
  var SMALL_ARTICLE_MIN_WORDS = 50;
  var SMALL_ARTICLE_MAX_WORDS = 130;
  var GROUP_GAP = 8;
  var HEADING_GAP = 0; // Automatically padded in object style
  var IMAGE_GAP = 6;
  var DEFAULT_IMAGE_ASPECT = 1.5; // width / height
  var IMAGE_SIDE_ALTERNATES = true;

  var STYLE_BODY_2 = "Body 2 Col";
  var PARA_HEADING_2 = "Heading 2";
  var PARA_HEADING = "Heading";
  var PARA_BODY = "Body";

  function getObjectStyle(name) {
    var style = app.activeDocument.objectStyles.itemByName(name);
    if (!style.isValid) {
      throw new Error("Object style not found: " + name);
    }
    return style;
  }

  function getParagraphStyle(name) {
    var style = app.activeDocument.paragraphStyles.itemByName(name);
    if (!style.isValid) {
      throw new Error("Paragraph style not found: " + name);
    }
    return style;
  }

  function applyParagraphStyle(frame, style) {
    if (!frame || !style) return;
    try {
      if (frame.parentStory && frame.parentStory.paragraphs.length > 0) {
        frame.parentStory.paragraphs.everyItem().appliedParagraphStyle = style;
      }
    } catch (e) {
      log("Could not apply paragraph style: " + e.message);
    }
  }

  function getPage(pageNumber, doc) {
    if (!pageNumber || pageNumber < 1) {
      throw new Error("Invalid page number: " + pageNumber);
    }
    while (doc.pages.length < pageNumber) doc.pages.add();
    return doc.pages.item(pageNumber - 1);
  }

  function getPageLayout(page) {
    var bounds = page.bounds;
    var margins = page.marginPreferences;
    var left = bounds[1] + margins.left;
    var right = bounds[3] - margins.right;
    var top = bounds[0] + margins.top;
    var bottom = bounds[2] - margins.bottom;
    var columnCount = Number(margins.columnCount) || EXPECTED_COLUMNS;
    var gutter = Number(margins.columnGutter) || 0;
    if (columnCount !== EXPECTED_COLUMNS) {
      throw new Error(
        "Page " +
          page.name +
          " is configured for " +
          columnCount +
          " columns. This layout engine expects " +
          EXPECTED_COLUMNS +
          " columns in Margins and Columns.",
      );
    }
    var usableWidth = right - left;
    var columnWidth = (usableWidth - gutter * (columnCount - 1)) / columnCount;
    return {
      top: top,
      left: left,
      right: right,
      bottom: bottom,
      width: usableWidth,
      height: bottom - top,
      columnCount: columnCount,
      gutter: gutter,
      columnWidth: columnWidth,
    };
  }

  function columnX(layout, columnIndex) {
    return layout.left + columnIndex * (layout.columnWidth + layout.gutter);
  }

  function spanWidth(layout, startColumn, spanColumns) {
    return spanColumns * layout.columnWidth + (spanColumns - 1) * layout.gutter;
  }

  function articleWords(article) {
    var count = Number(article.word_count);
    if (!count || count < 1) count = countWords(article.body || "");
    return Math.max(1, count);
  }

  function countWords(text) {
    var value = String(text || "")
      .replace(/\s+/g, " ")
      .replace(/^\s+|\s+$/g, "");
    if (!value) return 0;
    return value.split(" ").length;
  }

  function groupKey(article) {
    return (
      article.group_id ||
      article.group ||
      article.group_key ||
      article.layout_group ||
      null
    );
  }

  function isSmallArticle(article) {
    var wc = articleWords(article);
    return wc >= SMALL_ARTICLE_MIN_WORDS && wc <= SMALL_ARTICLE_MAX_WORDS;
  }

  // Grouping rules:
  // 1. An explicit group key on consecutive articles takes priority.
  // 2. Otherwise, two consecutive 50-180 word articles become a pair.
  // 3. All remaining articles are standard four-column units.
  function buildLayoutUnits(articles) {
    var units = [];
    var i = 0;
    while (i < articles.length) {
      var current = articles[i];
      var key = groupKey(current);
      if (key !== null && key !== undefined && i + 1 < articles.length) {
        var nextKey = groupKey(articles[i + 1]);
        if (String(key) === String(nextKey)) {
          var grouped = [current, articles[i + 1]];
          i += 2;
          while (
            i < articles.length &&
            String(groupKey(articles[i])) === String(key)
          ) {
            grouped.push(articles[i]);
            i++;
          }
          if (grouped.length === 2) {
            units.push(makePairUnit(grouped, "explicit"));
          } else {
            // The two-column design is specifically for pairs.
            // Larger explicit groups are safely laid out as singles.
            for (var g = 0; g < grouped.length; g++) {
              units.push(makeSingleUnit(grouped[g]));
            }
          }
          continue;
        }
      }
      if (
        isSmallArticle(current) &&
        i + 1 < articles.length &&
        isSmallArticle(articles[i + 1]) &&
        groupKey(articles[i + 1]) === null
      ) {
        units.push(makePairUnit([current, articles[i + 1]], "automatic"));
        i += 2;
        continue;
      }
      units.push(makeSingleUnit(current));
      i++;
    }
    return units;
  }

  function makeSingleUnit(article) {
    return {
      type: "single",
      articles: [article],
      words: articleWords(article),
      weight: articleWords(article) + 65,
    };
  }

  function makePairUnit(articles, reason) {
    var words = articleWords(articles[0]) + articleWords(articles[1]);
    return {
      type: "pair",
      articles: articles,
      words: words,
      weight: words + 90,
      grouping: reason,
    };
  }

  function headingHeight(article, availableHeight) {
    var text = String(article.heading || article.title || "");
    var estimatedLines = Math.max(1, Math.ceil(text.length / 75));
    var height = 25 + estimatedLines * 15;
    return Math.min(Math.max(28, height), Math.max(24, availableHeight * 0.35));
  }

  // Image column selection: short articles receive a wider image, which
  // occupies more vertical space. Longer articles receive narrower images.
  function imageColumnSpan(wordCount, maximumSpan) {
    var span;
    if (wordCount <= 180) span = 4;
    else if (wordCount <= 280) span = 3;
    else if (wordCount <= 380) span = 2;
    else span = 1;
    return Math.min(maximumSpan, Math.max(1, span));
  }

  function imageBounds(
    layout,
    bodyTop,
    bodyBottom,
    startColumn,
    spanColumns,
    side,
  ) {
    var width = spanWidth(layout, startColumn, spanColumns);
    var naturalHeight = width / DEFAULT_IMAGE_ASPECT;
    var availableHeight = Math.max(18, bodyBottom - bodyTop - IMAGE_GAP);
    var height = Math.min(naturalHeight, availableHeight);
    var left = columnX(layout, startColumn);
    if (side === "right") {
      left =
        columnX(layout, startColumn) +
        spanWidth(layout, startColumn, spanColumns) -
        width;
    }
    return [bodyTop, left, bodyTop + height, left + width];
  }

  function imageFileFor(article) {
    if (!article.cover || !article.cover.url) return null;
    var url = resolveImageURL(article.cover.url);
    return downloadImage(url, article.id || new Date().getTime());
  }

  // Place a heading frame at `top`, spanning `left`..`right`, and return
  // both the frame and the actual bottom edge after the story recomposes.
  function placeHeadingAndMeasure(
    page,
    top,
    left,
    right,
    text,
    objectStyle,
    paragraphStyle,
  ) {
    var probeBottom = top + 1; // any height; auto-size will fix it
    var frame = page.textFrames.add();
    frame.geometricBounds = [top, left, probeBottom, right];
    frame.contents = text || "";
    frame.applyObjectStyle(objectStyle, true);
    applyParagraphStyle(frame, paragraphStyle);
    try {
      frame.parentStory.recompose();
    } catch (e) {}
    var b;
    try {
      b = frame.visibleBounds;
    } catch (e) {
      b = frame.geometricBounds;
    }
    // visibleBounds = [y1, x1, y2, x2] in InDesign's ruler coords
    return { frame: frame, bottom: b[2] };
  }

  function placeSingleArticle(
    page,
    article,
    layout,
    top,
    bottom,
    styles,
    index,
  ) {
    var title = article.heading || article.title || "";
    var heading = placeHeadingAndMeasure(
      page,
      top,
      layout.left,
      layout.right,
      title,
      styles.headingObject,
      styles.headingParagraph,
    );
    var bodyTop = Math.min(bottom, heading.bottom);
    var bodyFrame = createTextFrame(
      page,
      [bodyTop, layout.left, bottom, layout.right],
      article.body || "",
      styles.body4,
      styles.bodyParagraph,
    );
    var imageFrame = null;
    if (article.cover && article.cover.url && bottom - bodyTop > 24) {
      var span = imageColumnSpan(articleWords(article), 4);
      var side = IMAGE_SIDE_ALTERNATES && index % 2 === 1 ? "right" : "left";
      var start = side === "right" ? 4 - span : 0;
      var bounds = imageBounds(layout, bodyTop, bottom, start, span, side);
      var file = imageFileFor(article);
      imageFrame = createImageFrame(page, bounds, file, styles.cover);
    }
    return { heading: heading.frame, body: bodyFrame, image: imageFrame };
  }

  function placePairArticle(
    page,
    article,
    layout,
    top,
    bottom,
    startColumn,
    styles,
  ) {
    var width = spanWidth(layout, startColumn, 2);
    var left = columnX(layout, startColumn);
    var heading = placeHeadingAndMeasure(
      page,
      top,
      left,
      left + width,
      article.heading || article.title || "",
      styles.headingObject,
      styles.headingParagraph2,
    );
    var bodyTop = Math.min(bottom, heading.bottom);
    var bodyFrame = createTextFrame(
      page,
      [bodyTop, left, bottom, left + width],
      article.body || "",
      styles.body2,
      styles.bodyParagraph,
    );
    var imageFrame = null;
    if (article.cover && article.cover.url && bottom - bodyTop > 24) {
      var bounds = imageBounds(layout, bodyTop, bottom, startColumn, 2, "left");
      var file = imageFileFor(article);
      imageFrame = createImageFrame(page, bounds, file, styles.cover);
    }
    return { heading: heading.frame, body: bodyFrame, image: imageFrame };
  }

  function placePairUnit(page, unit, layout, top, bottom, styles) {
    var leftArticle = unit.articles[0];
    var rightArticle = unit.articles[1];
    var leftFrames = placePairArticle(
      page,
      leftArticle,
      layout,
      top,
      bottom,
      0,
      styles,
    );
    var rightFrames = placePairArticle(
      page,
      rightArticle,
      layout,
      top,
      bottom,
      2,
      styles,
    );
    return [leftFrames, rightFrames];
  }

  function checkOverset(frames, pageNumber, article, oversetArticles) {
    if (!frames) return 0;
    var count = 0;
    var list = frames instanceof Array ? frames : [frames];
    for (var i = 0; i < list.length; i++) {
      var f = list[i];
      if (!f) continue;
      if (f.heading && f.heading.overflows) {
        count++;
        oversetArticles.push(
          "Page " + pageNumber + ": Heading - " + (article.title || article.id),
        );
      }
      if (f.body && f.body.overflows) {
        count++;
        oversetArticles.push(
          "Page " + pageNumber + ": Body - " + (article.title || article.id),
        );
      }
    }
    return count;
  }

  function activatePage(page) {
    if (!ACTIVATE_PAGE) return;
    try {
      var win = app.activeWindow;
      if (win && win.constructor.name === "LayoutWindow") {
        win.activePage = page;
        app.redraw(); // non-blocking repaint
      }
    } catch (e) {
      log("Could not activate page " + page.name + ": " + e.message);
    }
  }

  // =========================================================
  // MAIN
  // =========================================================

  try {
    if (app.documents.length === 0) {
      throw new Error("Please open an InDesign document first.");
    }
    var doc = app.activeDocument;
    var data = fetchPublicationData();
    var styles = {
      headingObject: getObjectStyle(STYLE_HEADING),
      cover: getObjectStyle(STYLE_COVER),
      body4: getObjectStyle(STYLE_BODY),
      body2: getObjectStyle(STYLE_BODY_2),
      headingParagraph2: getParagraphStyle(PARA_HEADING_2),
      headingParagraph: getParagraphStyle(PARA_HEADING),
      bodyParagraph: getParagraphStyle(PARA_BODY),
    };
    var totalPlaced = 0;
    var totalErrors = 0;
    var oversetCount = 0;
    var errors = [];
    var oversetArticles = [];
    var groupingReport = [];

    for (var p = 0; p < data.pages.length; p++) {
      var pageData = data.pages[p];
      var pageNumber = Number(pageData.page) || p + 1;
      log("Processing page " + pageNumber);

      var page = getPage(pageNumber, doc);
      var layout = getPageLayout(page);
      activatePage(page);

      var articles = pageData.articles || [];
      if (!articles.length) {
        log("Page " + pageNumber + " has no articles.");
        continue;
      }

      var units = buildLayoutUnits(articles);
      var totalWeight = 0;
      for (var u = 0; u < units.length; u++) totalWeight += units[u].weight;

      var currentY = layout.top;
      var interUnitGaps = Math.max(0, units.length - 1) * GROUP_GAP;
      var availableHeight = Math.max(1, layout.height - interUnitGaps);

      for (var u = 0; u < units.length; u++) {
        var unit = units[u];
        var unitHeight = availableHeight * (unit.weight / totalWeight);
        if (u === units.length - 1) unitHeight = layout.bottom - currentY;
        var unitTop = currentY;
        var unitBottom = Math.min(layout.bottom, unitTop + unitHeight);

        try {
          var frames;
          if (unit.type === "pair") {
            frames = placePairUnit(
              page,
              unit,
              layout,
              unitTop,
              unitBottom,
              styles,
            );
            groupingReport.push(
              "Page " +
                pageNumber +
                ": paired " +
                (unit.articles[0].title || unit.articles[0].id) +
                " + " +
                (unit.articles[1].title || unit.articles[1].id) +
                " (" +
                unit.grouping +
                ")",
            );
          } else {
            frames = placeSingleArticle(
              page,
              unit.articles[0],
              layout,
              unitTop,
              unitBottom,
              styles,
              u,
            );
          }

          for (var ai = 0; ai < unit.articles.length; ai++) {
            totalPlaced++;
            var articleFrames = unit.type === "pair" ? frames[ai] : frames;
            oversetCount += checkOverset(
              articleFrames,
              pageNumber,
              unit.articles[ai],
              oversetArticles,
            );
            log(
              "Placed: " +
                (unit.articles[ai].title || unit.articles[ai].id) +
                (unit.type === "pair" ? " [2-column pair]" : " [4-column]"),
            );
          }
        } catch (articleError) {
          totalErrors += unit.articles.length;
          errors.push(
            "Page " +
              pageNumber +
              " / Layout unit: " +
              (unit.articles[0].title || unit.articles[0].id) +
              "\n" +
              articleError.message,
          );
          log("ERROR: " + articleError.message);
        }

        currentY += unitHeight;
        if (u < units.length - 1) currentY += GROUP_GAP;
        if (currentY > layout.bottom) currentY = layout.bottom;
      }
    }

    var report =
      "NEWSPAPER LAYOUT COMPLETED\n\n" +
      "Generated: " +
      data.generated_at +
      "\n" +
      "Day: " +
      data.day +
      "\n\n" +
      "Pages processed: " +
      data.pages.length +
      "\n" +
      "Articles placed: " +
      totalPlaced +
      "\n" +
      "Errors: " +
      totalErrors +
      "\n" +
      "Overset frames: " +
      oversetCount +
      "\n\n" +
      "Unassigned articles: " +
      (data.unassigned_count || 0);

    if (groupingReport.length) {
      report += "\n\nAUTOMATIC ARTICLE GROUPING:\n" + groupingReport.join("\n");
    }
    if (oversetArticles.length) {
      report += "\n\nOVERSET TEXT:\n" + oversetArticles.join("\n");
    }
    if (errors.length) {
      report += "\n\nERROR DETAILS:\n" + errors.join("\n\n");
    }

    log(report);
    alert(report);
  } catch (e) {
    log("FATAL ERROR: " + e.message);
    alert("NEWSPAPER LAYOUT FAILED\n\n" + e.message);
  }
})();
