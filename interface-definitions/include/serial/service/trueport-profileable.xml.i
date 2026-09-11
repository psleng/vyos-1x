<!-- include start from serial/service/trueport-profileable.xml.i -->
<node name="trueport">
  <properties>
    <help>Trueport service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/serial-buffering.xml.i>
    <leafNode name="serial-buffering">
      <properties>
        <help>Enable serial buffering [trueport lite only]</help>
        <valueless/>
      </properties>
    </leafNode>
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
          <help>Trueport client settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/multihost.xml.i>
        #include <include/serial/service/utils/send-description.xml.i>
        <node name="remote">
          <properties>
            <help>Remote host connection settings</help>
          </properties>
          <children>
            <node name="backup">
              <properties>
                <help>Backup host connection settings [trueport lite only]</help>
              </properties>
            </node>
            <node name="multi-host">
              <properties>
                <help>Multi-host connection settings [trueport lite only]</help>
              </properties>
            </node>
          </children>
        </node>
      </children>
    </node>
    <node name="server">
       <properties>
          <help>Trueport server settings</help>
      </properties>
      <children>
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
