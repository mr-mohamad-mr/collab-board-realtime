/* Collab Board — API client (infrastructure layer).
   Single place that talks HTTP: token storage, auth headers, error
   mapping. Views never call fetch() directly. All JSON is parsed here;
   rendering layers only ever receive plain data objects. */

(function (global) {
  "use strict";

  var TOKEN_KEY = "collab.token";

  /** Error carrying the HTTP status so views can react (401 → re-login). */
  function ApiError(status, message) {
    var err = new Error(message || "Request failed");
    err.name = "ApiError";
    err.status = status;
    return err;
  }

  function createApi(baseUrl) {
    var base = (baseUrl || "").replace(/\/$/, "");

    function token() {
      try { return global.localStorage.getItem(TOKEN_KEY); }
      catch (e) { return null; }
    }

    function setToken(value) {
      try {
        if (value) { global.localStorage.setItem(TOKEN_KEY, value); }
        else { global.localStorage.removeItem(TOKEN_KEY); }
      } catch (e) { /* private mode: token lives in memory only */ }
      api._memoryToken = value || null;
    }

    function getToken() {
      return token() || api._memoryToken || null;
    }

    function headers(hasBody) {
      var h = {};
      if (hasBody) { h["Content-Type"] = "application/json"; }
      var t = getToken();
      if (t) { h["Authorization"] = "Bearer " + t; }
      return h;
    }

    function request(path, options) {
      options = options || {};
      return global.fetch(base + path, {
        method: options.method || "GET",
        headers: headers(Boolean(options.body)),
        body: options.body ? JSON.stringify(options.body) : undefined,
      }).then(function (resp) {
        if (resp.status === 204) { return null; }
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) {
            var detail = data && data.detail;
            var message = typeof detail === "string" ? detail : "Request failed (" + resp.status + ")";
            if (resp.status === 401) { setToken(null); }
            throw ApiError(resp.status, message);
          }
          return data;
        });
      });
    }

    var api = {
      _memoryToken: null,
      getToken: getToken,
      setToken: setToken,
      isLoggedIn: function () { return Boolean(getToken()); },

      register: function (username, password, displayName) {
        return request("/auth/register", {
          method: "POST",
          body: { username: username, password: password, display_name: displayName || "" },
        });
      },
      login: function (username, password) {
        return request("/auth/login", {
          method: "POST",
          body: { username: username, password: password },
        });
      },
      me: function () { return request("/auth/me"); },

      listBoards: function () { return request("/boards"); },
      createBoard: function (title, description) {
        return request("/boards", { method: "POST", body: { title: title, description: description || "" } });
      },
      getBoard: function (boardId) { return request("/boards/" + boardId); },
      updateBoard: function (boardId, patch) {
        return request("/boards/" + boardId, { method: "PATCH", body: patch });
      },
      deleteBoard: function (boardId) {
        return request("/boards/" + boardId, { method: "DELETE" });
      },
      listMembers: function (boardId) { return request("/boards/" + boardId + "/members"); },
      addMember: function (boardId, username, role) {
        return request("/boards/" + boardId + "/members", {
          method: "POST", body: { username: username, role: role || "editor" },
        });
      },
      updateMember: function (boardId, userId, role) {
        return request("/boards/" + boardId + "/members/" + userId, {
          method: "PATCH", body: { role: role },
        });
      },
      removeMember: function (boardId, userId) {
        return request("/boards/" + boardId + "/members/" + userId, { method: "DELETE" });
      },

      snapshot: function (boardId) { return request("/boards/" + boardId + "/snapshot"); },
      listActivity: function (boardId, limit, beforeId) {
        var q = "?limit=" + (limit || 50);
        if (beforeId) { q += "&before_id=" + beforeId; }
        return request("/boards/" + boardId + "/activity" + q);
      },
      listItems: function (boardId) { return request("/boards/" + boardId + "/items"); },
      createItem: function (boardId, item) {
        return request("/boards/" + boardId + "/items", { method: "POST", body: item });
      },
      updateItem: function (boardId, itemId, patch) {
        return request("/boards/" + boardId + "/items/" + itemId, { method: "PATCH", body: patch });
      },
      deleteItem: function (boardId, itemId) {
        return request("/boards/" + boardId + "/items/" + itemId, { method: "DELETE" });
      },
    };
    return api;
  }

  global.CollabApi = { createApi: createApi, ApiError: ApiError };
})(typeof window !== "undefined" ? window : globalThis);
