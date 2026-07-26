
local HttpService = game:GetService("HttpService")
local AssetService = game:GetService("AssetService")
local Workspace = game:GetService("Workspace")
local Players = game:GetService("Players")

local SERVER_URL = "http://localhost:5000"
local POLL_INTERVAL = 1
local SYNC_FOLDER_NAME = "BlenderLiveLink"

local SHARED_SECRET = ""

local LARGE_MESH_TRIANGLE_THRESHOLD = 20000
local TRIANGLE_BATCH_SIZE = 2500

local USE_MSGPACK_IF_AVAILABLE = true

local registry = {}
local lastRevision = 0
local lastSessionId = nil
local running = false

local MsgPack = {}

function MsgPack.decode(bytes)
    local pos = 1
    local len = #bytes

    local function byte()
        local b = string.byte(bytes, pos)
        pos = pos + 1
        return b
    end

    local function bytesTo(n)
        local s = string.sub(bytes, pos, pos + n - 1)
        pos = pos + n
        return s
    end

    local function readUInt(n)
        local v = 0
        for _ = 1, n do
            v = v * 256 + byte()
        end
        return v
    end

    local function readInt(n)
        local v = readUInt(n)
        local bits = n * 8
        local signBit = 2 ^ (bits - 1)
        if v >= signBit then
            v = v - (2 ^ bits)
        end
        return v
    end

    local function readFloat()
        local s = bytesTo(4)
        local b1, b2, b3, b4 = string.byte(s, 1, 4)
        local sign = (b1 > 127) and -1 or 1
        local exp = ((b1 % 128) * 2) + math.floor(b2 / 128)
        local mant = ((b2 % 128) * 65536) + (b3 * 256) + b4
        if exp == 0 then
            return sign * mant * 2 ^ (-149)
        end
        return sign * (1 + mant / 8388608) * 2 ^ (exp - 127)
    end

    local function readDouble()
        local s = bytesTo(8)
        local b = { string.byte(s, 1, 8) }
        local sign = (b[1] > 127) and -1 or 1
        local exp = ((b[1] % 128) * 16) + math.floor(b[2] / 16)
        local mant = (b[2] % 16) * 2 ^ 48
        mant = mant + b[3] * 2 ^ 40 + b[4] * 2 ^ 32 + b[5] * 2 ^ 24
             + b[6] * 2 ^ 16 + b[7] * 2 ^ 8 + b[8]
        if exp == 0 then
            return sign * mant * 2 ^ (-1074)
        end
        return sign * (1 + mant / 2 ^ 52) * 2 ^ (exp - 1023)
    end

    local decodeValue

    local function decodeArray(count)
        local t = {}
        for i = 1, count do
            t[i] = decodeValue()
        end
        return t
    end

    local function decodeMap(count)
        local t = {}
        for _ = 1, count do
            local k = decodeValue()
            local v = decodeValue()
            t[k] = v
        end
        return t
    end

    decodeValue = function()
        local b = byte()

        if b <= 0x7f then
            return b
        elseif b >= 0xe0 then
            return b - 256
        elseif b >= 0x80 and b <= 0x8f then
            return decodeMap(b - 0x80)
        elseif b >= 0x90 and b <= 0x9f then
            return decodeArray(b - 0x90)
        elseif b >= 0xa0 and b <= 0xbf then
            return bytesTo(b - 0xa0)
        elseif b == 0xc0 then
            return nil
        elseif b == 0xc2 then
            return false
        elseif b == 0xc3 then
            return true
        elseif b == 0xca then
            return readFloat()
        elseif b == 0xcb then
            return readDouble()
        elseif b == 0xcc then
            return readUInt(1)
        elseif b == 0xcd then
            return readUInt(2)
        elseif b == 0xce then
            return readUInt(4)
        elseif b == 0xcf then
            return readUInt(8)
        elseif b == 0xd0 then
            return readInt(1)
        elseif b == 0xd1 then
            return readInt(2)
        elseif b == 0xd2 then
            return readInt(4)
        elseif b == 0xd3 then
            return readInt(8)
        elseif b == 0xd9 then
            local n = readUInt(1)
            return bytesTo(n)
        elseif b == 0xda then
            local n = readUInt(2)
            return bytesTo(n)
        elseif b == 0xdb then
            local n = readUInt(4)
            return bytesTo(n)
        elseif b == 0xdc then
            local n = readUInt(2)
            return decodeArray(n)
        elseif b == 0xdd then
            local n = readUInt(4)
            return decodeArray(n)
        elseif b == 0xde then
            local n = readUInt(2)
            return decodeMap(n)
        elseif b == 0xdf then
            local n = readUInt(4)
            return decodeMap(n)
        elseif b == 0xc4 then
            local n = readUInt(1)
            return bytesTo(n)
        elseif b == 0xc5 then
            local n = readUInt(2)
            return bytesTo(n)
        elseif b == 0xc6 then
            local n = readUInt(4)
            return bytesTo(n)
        else
            error("MsgPack: unsupported type byte 0x" .. string.format("%02x", b))
        end
    end

    local ok, result = pcall(decodeValue)
    if not ok then
        return nil, result
    end
    return result, nil
end

local StatusIndicator = {}
StatusIndicator._label = nil

function StatusIndicator.init()
    local ok = pcall(function()
        local player = Players.LocalPlayer
        if not player then
            return
        end
        local playerGui = player:WaitForChild("PlayerGui", 5)
        if not playerGui then
            return
        end

        local existing = playerGui:FindFirstChild("BlenderLiveLinkStatusGui")
        if existing then
            existing:Destroy()
        end

        local screenGui = Instance.new("ScreenGui")
        screenGui.Name = "BlenderLiveLinkStatusGui"
        screenGui.ResetOnSpawn = false
        screenGui.IgnoreGuiInset = true
        screenGui.DisplayOrder = 1000

        local frame = Instance.new("Frame")
        frame.Name = "StatusFrame"
        frame.AnchorPoint = Vector2.new(1, 1)
        frame.Position = UDim2.new(1, -12, 1, -12)
        frame.Size = UDim2.new(0, 260, 0, 32)
        frame.BackgroundColor3 = Color3.fromRGB(30, 30, 30)
        frame.BackgroundTransparency = 0.25
        frame.BorderSizePixel = 0
        frame.Parent = screenGui

        local corner = Instance.new("UICorner")
        corner.CornerRadius = UDim.new(0, 6)
        corner.Parent = frame

        local label = Instance.new("TextLabel")
        label.Name = "StatusLabel"
        label.BackgroundTransparency = 1
        label.Size = UDim2.new(1, -12, 1, 0)
        label.Position = UDim2.new(0, 6, 0, 0)
        label.Font = Enum.Font.Gotham
        label.TextSize = 14
        label.TextXAlignment = Enum.TextXAlignment.Left
        label.TextColor3 = Color3.fromRGB(255, 255, 255)
        label.Text = "\u{1F7E1} Live Link: Idle"
        label.Parent = frame

        screenGui.Parent = playerGui
        StatusIndicator._label = label
    end)
    return ok
end

function StatusIndicator.set(state, message)
    if not StatusIndicator._label then
        return
    end
    local icons = {
        synced = "\u{1F7E2}",
        syncing = "\u{1F7E1}",
        error = "\u{1F534}",
        idle = "\u{1F7E1}",
    }
    local icon = icons[state] or "\u{26AA}"
    local ok = pcall(function()
        StatusIndicator._label.Text = icon .. " Live Link: " .. (message or state)
    end)
    if not ok then
        StatusIndicator._label = nil
    end
end

local function getSyncFolder()
    local folder = Workspace:FindFirstChild(SYNC_FOLDER_NAME)
    if not folder then
        folder = Instance.new("Folder")
        folder.Name = SYNC_FOLDER_NAME
        folder.Parent = Workspace
    end
    return folder
end

local function toVector3(t)
    return Vector3.new(t[1], t[2], t[3])
end

local function toColor3(t)
    return Color3.new(t[1] or 1, t[2] or 1, t[3] or 1)
end

local function toAlpha(t)
    return t[4] or 1
end

local function destroyEntry(entry)
    pcall(function()
        entry.meshPart:Destroy()
    end)
    pcall(function()
        entry.editableMesh:Destroy()
    end)
end

local function removeObject(name)
    local entry = registry[name]
    if not entry then
        return
    end
    destroyEntry(entry)
    registry[name] = nil
    print(("[LiveLink] Removed '%s' (deleted on the Blender side)"):format(name))
end

local function createObject(name, vertices, triangles, normals, triangleNormals, colors, triangleColors, uvs, triangleUvs)
    local editableMesh = AssetService:CreateEditableMesh()

    local triCount = #triangles
    local isLargeMesh = triCount > LARGE_MESH_TRIANGLE_THRESHOLD
    if isLargeMesh then
        warn(("[LiveLink] '%s' has %d triangles (over the %d-triangle guard threshold) - "
            .. "building in batches to avoid freezing Studio"):format(name, triCount, LARGE_MESH_TRIANGLE_THRESHOLD))
        StatusIndicator.set("syncing", ("Building large mesh '%s' (%d tris)"):format(name, triCount))
    end

    local vertexIdMap = {}
    for i, posTable in ipairs(vertices) do
        local vid = editableMesh:AddVertex(toVector3(posTable))
        vertexIdMap[i - 1] = vid
    end

    local normalIdMap = {}
    if type(normals) == "table" then
        for i, normTable in ipairs(normals) do
            local nid = editableMesh:AddNormal(toVector3(normTable))
            normalIdMap[i - 1] = nid
        end
    end

    local uvIdMap = {}
    local hasUvs = type(uvs) == "table"
    if hasUvs then
        for i, uvTable in ipairs(uvs) do
            local ok, uid = pcall(function()
                return editableMesh:AddUV(Vector2.new(uvTable[1], uvTable[2]))
            end)
            if ok then
                uvIdMap[i - 1] = uid
            else
                warn(("[LiveLink] Failed to add UV #%d for '%s'"):format(i - 1, name))
                hasUvs = false
                break
            end
        end
    end

    local colorIdMap = {}
    local hasColors = type(colors) == "table"
    if hasColors then
        for i, colTable in ipairs(colors) do
            local ok, cid = pcall(function()
                return editableMesh:AddColor(toColor3(colTable), toAlpha(colTable))
            end)
            if ok then
                colorIdMap[i - 1] = cid
            else
                warn(("[LiveLink] Failed to add color #%d for '%s'"):format(i - 1, name))
                hasColors = false
                break
            end
        end
    end

    local hasTriangleNormals = type(triangleNormals) == "table"
    local hasTriangleUvs = hasUvs and type(triangleUvs) == "table"
    local hasTriangleColors = hasColors and type(triangleColors) == "table"

    for triIndex, tri in ipairs(triangles) do
        local id0 = vertexIdMap[tri[1]]
        local id1 = vertexIdMap[tri[2]]
        local id2 = vertexIdMap[tri[3]]
        if id0 and id1 and id2 then
            local faceId = editableMesh:AddTriangle(id0, id1, id2)

            if hasTriangleNormals then
                local triNorm = triangleNormals[triIndex]
                if triNorm then
                    local n0 = normalIdMap[triNorm[1]]
                    local n1 = normalIdMap[triNorm[2]]
                    local n2 = normalIdMap[triNorm[3]]
                    if n0 and n1 and n2 then
                        editableMesh:SetFaceNormals(faceId, { n0, n1, n2 })
                    end
                end
            end

            if hasTriangleUvs then
                local triUv = triangleUvs[triIndex]
                if triUv then
                    local u0 = uvIdMap[triUv[1]]
                    local u1 = uvIdMap[triUv[2]]
                    local u2 = uvIdMap[triUv[3]]
                    if u0 and u1 and u2 then
                        pcall(function()
                            editableMesh:SetFaceUVs(faceId, { u0, u1, u2 })
                        end)
                    end
                end
            end

            if hasTriangleColors then
                local triCol = triangleColors[triIndex]
                if triCol then
                    local c0 = colorIdMap[triCol[1]]
                    local c1 = colorIdMap[triCol[2]]
                    local c2 = colorIdMap[triCol[3]]
                    if c0 and c1 and c2 then
                        pcall(function()
                            editableMesh:SetFaceColors(faceId, { c0, c1, c2 })
                        end)
                    end
                end
            end
        end

        if isLargeMesh and triIndex % TRIANGLE_BATCH_SIZE == 0 then
            task.wait()
        end
    end

    local ok, meshPartOrErr = pcall(function()
        return AssetService:CreateMeshPartAsync(
            Content.fromObject(editableMesh),
            {
                CollisionFidelity = Enum.CollisionFidelity.Default,
                RenderFidelity = Enum.RenderFidelity.Precise,
            }
        )
    end)

    if not ok then
        warn(("[LiveLink] Failed to create MeshPart for '%s': %s"):format(name, tostring(meshPartOrErr)))
        editableMesh:Destroy()
        StatusIndicator.set("error", ("Failed to build '%s'"):format(name))
        return
    end

    local meshPart = meshPartOrErr
    meshPart.Name = name
    meshPart.Anchored = true
    meshPart.CanCollide = false
    meshPart.CanTouch = false
    meshPart.Material = Enum.Material.SmoothPlastic

    if not hasColors and type(colors) == "table" and colors[1] then
        pcall(function()
            meshPart.Color = toColor3(colors[1])
        end)
    end

    meshPart.Parent = getSyncFolder()

    registry[name] = {
        meshPart = meshPart,
        editableMesh = editableMesh,
        vertexIdMap = vertexIdMap,
        normalIdMap = normalIdMap,
        uvIdMap = uvIdMap,
        colorIdMap = colorIdMap,
        vertexCount = #vertices,
        normalCount = (type(normals) == "table") and #normals or 0,
        uvCount = hasUvs and #uvs or 0,
        colorCount = hasColors and #colors or 0,
    }

    print(("[LiveLink] Created '%s' (%d verts, %d tris, %d normals, %d uvs, %d colors)"):format(
        name, #vertices, #triangles,
        (type(normals) == "table") and #normals or 0,
        hasUvs and #uvs or 0,
        hasColors and #colors or 0
        ))
end

local function updateObject(entry, vertices, normals, colors, uvs)
    local editableMesh = entry.editableMesh

    for i, posTable in ipairs(vertices) do
        local vid = entry.vertexIdMap[i - 1]
        if vid then
            editableMesh:SetPosition(vid, toVector3(posTable))
        end
    end

    if type(normals) == "table" then
        for i, normTable in ipairs(normals) do
            local nid = entry.normalIdMap[i - 1]
            if nid then
                editableMesh:SetNormal(nid, toVector3(normTable))
            end
        end
    end

    if type(uvs) == "table" and entry.uvIdMap then
        for i, uvTable in ipairs(uvs) do
            local uid = entry.uvIdMap[i - 1]
            if uid then
                pcall(function()
                    editableMesh:SetUV(uid, Vector2.new(uvTable[1], uvTable[2]))
                end)
            end
        end
    end

    if type(colors) == "table" and entry.colorIdMap then
        for i, colTable in ipairs(colors) do
            local cid = entry.colorIdMap[i - 1]
            if cid then
                pcall(function()
                    editableMesh:SetColor(cid, toColor3(colTable), toAlpha(colTable))
                end)
            end
        end
    elseif type(colors) == "table" and colors[1] and (not entry.colorIdMap or #entry.colorIdMap == 0) then
        pcall(function()
            entry.meshPart.Color = toColor3(colors[1])
        end)
    end

    local ok, tempPart = pcall(function()
        return AssetService:CreateMeshPartAsync(
            Content.fromObject(editableMesh),
            {
                CollisionFidelity = Enum.CollisionFidelity.Default,
                RenderFidelity = Enum.RenderFidelity.Precise,
            }
        )
    end)

    if ok and tempPart then
        local applied = pcall(function()
            entry.meshPart:ApplyMesh(tempPart)
        end)
        tempPart:Destroy()
        if not applied then
            warn(("[LiveLink] Visuals updated for '%s', but ApplyMesh failed (collision may be stale)"):format(entry.meshPart.Name))
        end
    else
        warn(("[LiveLink] Could not refresh collision for '%s': %s"):format(entry.meshPart.Name, tostring(tempPart)))
    end
end

local function processObject(data)
    local name = data.name
    local vertices = data.vertices
    local triangles = data.triangles
    local normals = data.normals
    local triangleNormals = data.triangle_normals
    local colors = data.colors
    local triangleColors = data.triangle_colors
    local uvs = data.uvs
    local triangleUvs = data.triangle_uvs

    if type(name) ~= "string" or type(vertices) ~= "table" or type(triangles) ~= "table" then
        warn("[LiveLink] Received a malformed object payload, skipping")
        return
    end

    local existing = registry[name]
    local incomingNormalCount = (type(normals) == "table") and #normals or 0
    local incomingUvCount = (type(uvs) == "table") and #uvs or 0

    if existing and existing.vertexCount == #vertices
        and existing.normalCount == incomingNormalCount
        and existing.uvCount == incomingUvCount then
        updateObject(existing, vertices, normals, colors, uvs)
    else
        if existing then
            destroyEntry(existing)
            registry[name] = nil
        end
        createObject(name, vertices, triangles, normals, triangleNormals, colors, triangleColors, uvs, triangleUvs)
    end
end

local function decodeResponse(response, contentType)
    if USE_MSGPACK_IF_AVAILABLE and contentType and contentType:find("msgpack") then
        local decoded, err = MsgPack.decode(response)
        if decoded ~= nil then
            return true, decoded
        end
        warn("[LiveLink] MsgPack decode failed, falling back to JSON parse attempt: " .. tostring(err))
    end

    local decodeOk, decoded = pcall(function()
        return HttpService:JSONDecode(response)
    end)
    if not decodeOk then
        return false, nil
    end
    return true, decoded
end

local function poll()
    local url = SERVER_URL .. "/objects?since=" .. tostring(lastRevision)

    StatusIndicator.set("syncing", "Polling server...")

    local requestHeaders = { ["Accept"] = "application/json" }
    if USE_MSGPACK_IF_AVAILABLE then
        requestHeaders = { ["Accept"] = "application/x-msgpack, application/json" }
    end
    if SHARED_SECRET ~= "" then
        requestHeaders["X-LiveLink-Token"] = SHARED_SECRET
    end

    local ok, response = pcall(function()
        return HttpService:RequestAsync({
            Url = url,
            Method = "GET",
            Headers = requestHeaders,
        })
    end)

    if not ok then
        warn("[LiveLink] Could not reach bridge server (" .. SERVER_URL .. "): " .. tostring(response))
        StatusIndicator.set("error", "Server Offline / Error")
        return
    end

    if not response or not response.Success then
        local code = response and response.StatusCode
        if code == 401 then
            warn("[LiveLink] Server rejected request: unauthorized (401). Check SHARED_SECRET matches the server's LIVELINK_SHARED_SECRET.")
            StatusIndicator.set("error", "Unauthorized - check SHARED_SECRET")
        else
            warn("[LiveLink] Server error (" .. tostring(code) .. "): " .. tostring(response and response.Body))
            StatusIndicator.set("error", "Server Error")
        end
        return
    end

    local contentType = response.Headers and response.Headers["content-type"]
    local decodeOk, decoded = decodeResponse(response.Body, contentType)

    if not decodeOk or type(decoded) ~= "table" then
        warn("[LiveLink] Received invalid/undecodable response from server")
        StatusIndicator.set("error", "Bad response from server")
        return
    end

    local incomingSessionId = decoded.session_id
    local serverRestarted = incomingSessionId ~= nil
        and lastSessionId ~= nil
        and incomingSessionId ~= lastSessionId

    if incomingSessionId ~= nil then
        lastSessionId = incomingSessionId
    end

    if serverRestarted then
        warn("[LiveLink] Bridge server restarted - forcing full resync")
        lastRevision = 0
    else
        lastRevision = decoded.revision or lastRevision
    end

    for _, deletedName in ipairs(decoded.deleted or {}) do
        removeObject(deletedName)
    end

    local objectCount = #(decoded.objects or {})
    for _, objData in ipairs(decoded.objects or {}) do
        local processOk, err = pcall(processObject, objData)
        if not processOk then
            warn(("[LiveLink] Error processing '%s': %s"):format(tostring(objData.name), tostring(err)))
            StatusIndicator.set("error", ("Error processing '%s'"):format(tostring(objData.name)))
        end
    end

    if objectCount > 0 then
        StatusIndicator.set("synced", ("Synced (%d updated)"):format(objectCount))
    else
        StatusIndicator.set("synced", "Synced")
    end
end

local toolbar = plugin:CreateToolbar("Blender Live Link")
local toggleButton = toolbar:CreateButton(
    "ToggleSync",
    "Start/stop polling the local bridge server for mesh updates",
    ""
)
toggleButton.ClickableWhenViewportHidden = true

local function setRunning(state)
    running = state
    toggleButton:SetActive(running)

    if running then
        print("[LiveLink] Sync started - polling " .. SERVER_URL .. " every " .. POLL_INTERVAL .. "s")
        StatusIndicator.init()
        StatusIndicator.set("syncing", "Starting...")
        task.spawn(function()
            while running do
                local ok, err = pcall(poll)
                if not ok then
                    warn("[LiveLink] Poll error: " .. tostring(err))
                    StatusIndicator.set("error", "Poll error")
                end
                task.wait(POLL_INTERVAL)
            end
        end)
    else
        print("[LiveLink] Sync stopped")
        StatusIndicator.set("idle", "Stopped")
    end
end

toggleButton.Click:Connect(function()
    setRunning(not running)
end)

plugin.Unloading:Connect(function()
    running = false
end)
