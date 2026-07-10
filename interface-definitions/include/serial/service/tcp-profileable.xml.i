<!-- include start from serial/service/tcp-profileable.xml.i -->
<node name="tcp">
  <properties>
    <help>TCP service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/serial-buffering.xml.i>
    #include <include/serial/service/utils/idle-timeout.xml.i>
    #include <include/serial/service/utils/keepalive.xml.i>
    #include <include/serial/service/utils/session-timeout.xml.i>
    #include <include/serial/service/utils/modem.xml.i>
    #include <include/serial/service/utils/tls-port.xml.i>
    #include <include/serial/service/utils/transmit-string-all.xml.i>
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
        #include <include/serial/service/utils/auth-user.xml.i>
        #include <include/serial/service/utils/listen-port.xml.i>
        #include <include/serial/service/utils/banner.xml.i>
        #include <include/serial/service/utils/motd.xml.i>
        <leafNode name="allow-multiple-connections">
          <properties>
            <help>Enable allow multiple connections</help>
            <valueless/>
          </properties>
        </leafNode>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
