<!-- include start from serial/service/trueport.xml.i -->
<node name="trueport">
  <properties>
    <help>Trueport service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/serial-buffering.xml.i>
    #include <include/serial/service/utils/idle-timeout.xml.i>
    #include <include/serial/service/utils/keepalive.xml.i>
    #include <include/serial/service/utils/session-timeout.xml.i>
    #include <include/serial/service/utils/tls-port.xml.i>
    #include <include/serial/service/utils/transmit-string-all.xml.i>
    <leafNode name="signal-active">
      <properties>
        <help>Enable raise signals when not under trueport client control</help>
        <valueless/>
      </properties>
    </leafNode>
    <node name="client">
      <properties>
          <help>TCP client settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/initiate.xml.i>
        #include <include/serial/service/utils/multihost.xml.i>
        #include <include/serial/service/utils/send-description.xml.i>
      </children>
    </node>
    <node name="server">
       <properties>
          <help>TCP server settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/listen-port.xml.i>
        <leafNode name="allow-multiple-connections">
          <properties>
            <help>Enable allow multiple connections [trueport lite only]</help>
            <valueless/>
          </properties>
        </leafNode>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
