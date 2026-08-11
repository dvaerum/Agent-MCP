"use client"

import { useState } from "react"
import { Plus } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { toast, toastError } from "@/components/ui/toast"
import { useServerStore } from "@/lib/stores/server-store"

export function ManualServerInput() {
  const [host, setHost] = useState("localhost")
  const [port, setPort] = useState("")
  const [name, setName] = useState("")
  const { addServer, setActiveServer } = useServerStore()
  const [isAdding, setIsAdding] = useState(false)

  const handleAddServer = async () => {
    if (!port || !name) return
    
    setIsAdding(true)
    const portNumber = parseInt(port)
    
    if (isNaN(portNumber) || portNumber < 1 || portNumber > 65535) {
      // AX-5: shared toast (role=alert + aria-live) instead of the
      // native, unannounceable window.alert().
      toast({
        variant: "error",
        description: "Please enter a valid port number (1-65535).",
      })
      setIsAdding(false)
      return
    }

    try {
      // Add the server
      const newServer = {
        name,
        host,
        port: portNumber,
        description: `Custom server on port ${portNumber}`
      }
      
      addServer(newServer)
      
      // Try to connect immediately
      const servers = useServerStore.getState().servers
      const addedServer = servers[servers.length - 1]
      if (addedServer) {
        await setActiveServer(addedServer.id)
      }
      
      // Reset form
      setName("")
      setPort("")
    } catch (error) {
      toastError(error, "Failed to add server")
    } finally {
      setIsAdding(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label htmlFor="name">Server Name</Label>
          <Input
            id="name"
            placeholder="My MCP Server"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="port">Port</Label>
          <Input
            id="port"
            type="number"
            placeholder="8080"
            value={port}
            onChange={(e) => setPort(e.target.value)}
          />
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="host">Host (optional)</Label>
        <Input
          id="host"
          placeholder="localhost"
          value={host}
          onChange={(e) => setHost(e.target.value)}
        />
      </div>
      <Button 
        onClick={handleAddServer} 
        disabled={!port || !name || isAdding}
        className="w-full"
      >
        <Plus className="w-4 h-4 mr-2" />
        {isAdding ? "Connecting..." : "Add Server"}
      </Button>
    </div>
  )
}